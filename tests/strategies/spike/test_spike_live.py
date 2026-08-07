import asyncio
from dataclasses import replace
from decimal import Decimal
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError

from trading_platform.shared.binance.strategy_account import BinanceStrategyAccount
from trading_platform.shared.events import Bar1s, OrderIntent, StrategyAuditEvent
from trading_platform.shared.execution_recovery import OrderWAL
from trading_platform.shared.risk import RiskConfig, RiskGuard
from trading_platform.strategies.spike_live import (
    LIVE_CONFIRMATION,
    CompositeEntryGate,
    SpikeExecutionCoordinator,
    SpikeLiveSettings,
    SpikeRuntimeCallbacks,
    require_one_way_position_mode,
)
from trading_platform.strategies.campaign_store import CampaignLease
from trading_platform.strategies.spike_main import (
    SpikeLiveProcess,
    require_viable_entry_notional,
)


class StrategyStub:
    def __init__(self):
        self.enabled = None
        self.strategies = {"BTCUSDT": object()}

    def set_entry_enabled(self, enabled):
        self.enabled = enabled

    def drain_audit_events(self):
        return []


class AuditedStrategyStub(StrategyStub):
    def __init__(self, events):
        super().__init__()
        self.audit_events = list(events)

    def drain_audit_events(self):
        events = self.audit_events
        self.audit_events = []
        return events


def _entry(tier=1):
    return OrderIntent(
        symbol="BTCUSDT",
        side="SELL",
        price=Decimal("100"),
        quantity=Decimal("1"),
        client_order_id=f"spike_short_BTCUSDT_1000_tier{tier}",
        ttl_ms=None,
        reduce_only=False,
        strategy_id="spike_short",
        trigger_reason=f"spike_tier{tier}",
    )


@pytest.mark.asyncio
async def test_audit_failure_retains_events_and_closes_execution_until_retry():
    event = StrategyAuditEvent(
        event_time=1_000,
        event_type="signal_triggered",
        symbol="BTCUSDT",
        strategy_id="spike_short",
        campaign_id="spike_short:BTCUSDT:1000",
        details={"trigger_price": "100"},
    )
    strategy = AuditedStrategyStub([event])
    gate = CompositeEntryGate(strategy)
    gate.set_condition("execution", True)
    risk = RiskGuard("spike-test", RiskConfig())
    sink = AsyncMock(side_effect=[RuntimeError("postgres unavailable"), None])
    coordinator = SpikeExecutionCoordinator(
        strategy=strategy,
        account=Mock(),
        executor=Mock(),
        campaign_store=Mock(),
        risk_guard=risk,
        gate=gate,
        account_id="spike-test",
        audit_sink=sink,
    )

    assert await coordinator._publish_audit() is False
    assert gate.condition("execution") is False
    assert risk.halted is True
    assert sink.await_args.args == ((event,),)

    assert await coordinator._publish_audit() is True
    assert sink.await_count == 2
    assert sink.await_args.args == ((event,),)


@pytest.mark.asyncio
async def test_audit_events_are_not_drained_without_a_sink():
    event = StrategyAuditEvent(
        event_time=1_000,
        event_type="signal_triggered",
        symbol="BTCUSDT",
        strategy_id="spike_short",
        campaign_id=None,
        details={},
    )
    strategy = AuditedStrategyStub([event])
    coordinator = SpikeExecutionCoordinator(
        strategy=strategy,
        account=Mock(),
        executor=Mock(),
        campaign_store=Mock(),
        risk_guard=RiskGuard("spike-test", RiskConfig()),
        gate=CompositeEntryGate(strategy),
        account_id="spike-test",
    )

    assert await coordinator._publish_audit() is True
    assert strategy.audit_events == [event]


@pytest.mark.asyncio
async def test_campaign_acquire_and_recovery_emit_lifecycle_audit_events():
    campaign_id = "spike_short:BTCUSDT:1000"
    acquired_sink = AsyncMock()
    acquired_store = Mock(
        get_active=AsyncMock(return_value=None),
        acquire=AsyncMock(return_value=True),
    )
    acquired_strategy = StrategyStub()
    acquired = SpikeExecutionCoordinator(
        strategy=acquired_strategy,
        account=Mock(),
        executor=Mock(),
        campaign_store=acquired_store,
        risk_guard=RiskGuard("spike-test", RiskConfig()),
        gate=CompositeEntryGate(acquired_strategy),
        account_id="spike-test",
        audit_sink=acquired_sink,
    )

    assert await acquired._acquire_campaign(campaign_id, "BTCUSDT", 1_001)
    assert await acquired._publish_audit()
    acquired_sink.assert_awaited_once_with(
        (
            StrategyAuditEvent(
                event_time=1_001,
                event_type="campaign_acquired",
                symbol="BTCUSDT",
                strategy_id="spike_short",
                campaign_id=campaign_id,
                details={},
            ),
        )
    )

    recovered_sink = AsyncMock()
    lease = CampaignLease(
        campaign_id,
        "spike_short",
        "BTCUSDT",
        1_001,
        origin_price="100",
    )
    recovered_strategy = StrategyStub()
    recovered = SpikeExecutionCoordinator(
        strategy=recovered_strategy,
        account=Mock(symbols_with_live_risk=Mock(return_value=set())),
        executor=Mock(),
        campaign_store=Mock(get_active=AsyncMock(return_value=lease)),
        risk_guard=RiskGuard("spike-test", RiskConfig()),
        gate=CompositeEntryGate(recovered_strategy),
        account_id="spike-test",
        audit_sink=recovered_sink,
        now_ms=lambda: 2_000,
    )

    await recovered.restore_campaign_gate()
    assert await recovered._publish_audit()
    recovered_sink.assert_awaited_once_with(
        (
            StrategyAuditEvent(
                event_time=2_000,
                event_type="campaign_recovered",
                symbol="BTCUSDT",
                strategy_id="spike_short",
                campaign_id=campaign_id,
                details={},
            ),
        )
    )


@pytest.mark.asyncio
async def test_campaign_exit_state_change_emits_audit_with_persisted_state():
    campaign_id = "spike_short:BTCUSDT:1000"
    strategy = StrategyStub()
    strategy.campaign_exit_state = Mock(return_value=(True, True, False))
    sink = AsyncMock()
    store = Mock(update_exit_state=AsyncMock(return_value=True))
    coordinator = SpikeExecutionCoordinator(
        strategy=strategy,
        account=Mock(),
        executor=Mock(),
        campaign_store=store,
        risk_guard=RiskGuard("spike-test", RiskConfig()),
        gate=CompositeEntryGate(strategy),
        account_id="spike-test",
        audit_sink=sink,
        now_ms=lambda: 3_000,
    )
    coordinator._owned_campaign_id = campaign_id
    coordinator._owned_campaign_lease = CampaignLease(
        campaign_id, "spike_short", "BTCUSDT", 1_001
    )

    await coordinator._persist_exit_state("BTCUSDT")
    assert await coordinator._publish_audit()

    store.update_exit_state.assert_awaited_once_with(
        campaign_id,
        origin_checked=True,
        reduced_at_origin=True,
        exit_requested=False,
    )
    sink.assert_awaited_once_with(
        (
            StrategyAuditEvent(
                event_time=3_000,
                event_type="campaign_exit_state_changed",
                symbol="BTCUSDT",
                strategy_id="spike_short",
                campaign_id=campaign_id,
                details={
                    "origin_checked": True,
                    "reduced_at_origin": True,
                    "exit_requested": False,
                },
            ),
        )
    )


@pytest.mark.asyncio
async def test_campaign_release_audit_failure_halts_and_retries_same_event():
    campaign_id = "spike_short:BTCUSDT:1000"
    strategy = StrategyStub()
    gate = CompositeEntryGate(strategy)
    gate.set_condition("execution", True)
    gate.set_condition("campaign", False)
    risk = RiskGuard("spike-test", RiskConfig())
    sink = AsyncMock(side_effect=[RuntimeError("postgres unavailable"), None])
    store = Mock(release=AsyncMock(return_value=True))
    coordinator = SpikeExecutionCoordinator(
        strategy=strategy,
        account=Mock(
            has_open_position=Mock(return_value=False),
            has_pending_position_update=Mock(return_value=False),
            all_orders_terminal=Mock(return_value=True),
        ),
        executor=Mock(),
        campaign_store=store,
        risk_guard=risk,
        gate=gate,
        account_id="spike-test",
        audit_sink=sink,
        now_ms=lambda: 4_000,
    )
    coordinator._owned_campaign_id = campaign_id
    coordinator._owned_campaign_lease = CampaignLease(
        campaign_id, "spike_short", "BTCUSDT", 1_001
    )
    expected = StrategyAuditEvent(
        event_time=4_000,
        event_type="campaign_released",
        symbol="BTCUSDT",
        strategy_id="spike_short",
        campaign_id=campaign_id,
        details={},
    )

    assert await coordinator.maybe_release_campaign("BTCUSDT") is True
    store.release.assert_awaited_once_with(campaign_id)
    assert coordinator._owned_campaign_id is None
    assert gate.condition("campaign") is True
    assert gate.condition("execution") is False
    assert risk.halted is True
    allowed, reason = risk.check_can_open("BTCUSDT", Decimal("10"))
    assert allowed is False
    assert "strategy audit write failed: RuntimeError" in reason
    sink.assert_awaited_once_with((expected,))

    assert await coordinator._publish_audit() is True
    assert sink.await_count == 2
    assert sink.await_args_list[1].args == ((expected,),)
    assert coordinator._pending_audit_events == ()


def test_settings_default_to_testnet_and_live_requires_exact_confirmation():
    settings = SpikeLiveSettings(
        account_id="spike-test", symbols="btcusdt", total_notional="100"
    )
    assert settings.mode == "testnet"
    assert settings.exit_policy == "candidate-v1"
    assert settings.symbols == ["BTCUSDT"]

    with pytest.raises(ValidationError, match="confirmation"):
        SpikeLiveSettings(
            mode="live",
            account_id="spike-live",
            symbols=["BTCUSDT"],
            total_notional="100",
        )

    with pytest.raises(ValidationError, match="exit policy"):
        SpikeLiveSettings(
            mode="live",
            live_confirmation=LIVE_CONFIRMATION,
            account_id="spike-live",
            symbols=["BTCUSDT"],
            total_notional="100",
        )


def test_live_process_requires_explicit_one_way_position_mode():
    require_one_way_position_mode({"dualSidePosition": False})
    with pytest.raises(RuntimeError, match="one-way"):
        require_one_way_position_mode({"dualSidePosition": True})
    with pytest.raises(RuntimeError, match="one-way"):
        require_one_way_position_mode({})


def test_entry_notional_must_leave_every_tier_above_exchange_minimum():
    rules = Mock(symbol="AKEUSDT", min_notional=Decimal("5"))
    with pytest.raises(ValueError, match="smallest entry tier"):
        require_viable_entry_notional(Decimal("10"), rules)
    require_viable_entry_notional(Decimal("20"), rules)


def test_process_rejects_conflicting_account_configuration():
    settings = SpikeLiveSettings(
        account_id="spike-test", symbols=["AKEUSDT"], total_notional="10"
    )
    with pytest.raises(ValueError, match="must match"):
        SpikeLiveProcess(
            settings,
            binance=Mock(),
            database=Mock(),
            redis_config=Mock(),
            strategy_config=Mock(account_id="other-account"),
        )


@pytest.mark.asyncio
async def test_process_starts_bar_consumer_before_waiting_for_market_quality():
    settings = SpikeLiveSettings(
        account_id="spike-test", symbols=["AKEUSDT"], total_notional="10"
    )
    process = SpikeLiveProcess(
        settings,
        binance=Mock(),
        database=Mock(),
        redis_config=Mock(),
        strategy_config=Mock(account_id="spike-test"),
    )
    events = []
    blocker = asyncio.Event()
    account = Mock(
        refresh_positions=AsyncMock(),
        has_unresolved_orders=Mock(return_value=False),
    )
    coordinator = Mock(
        restore_campaign_gate=AsyncMock(),
        account=account,
        reconcile_entry_expirations=AsyncMock(),
        validate_recovered_campaign=Mock(),
        restore_campaign_timing=AsyncMock(),
        maybe_release_campaign=AsyncMock(),
        _flush_cancellations=AsyncMock(),
        stop=AsyncMock(),
    )
    runtime = Mock(start=AsyncMock(), stop=AsyncMock())
    gate = Mock(set_condition=Mock())
    admission = Mock(on_universe_scan=AsyncMock())

    async def build_resources():
        process.coordinator = coordinator
        process.runtime = runtime
        process.gate = gate
        process.admission = admission

    process._build_resources = AsyncMock(side_effect=build_resources)
    process._register_market_subscriptions = AsyncMock(
        side_effect=lambda: events.append("registered")
    )
    process._warm_strategy_history = AsyncMock(
        side_effect=lambda: events.append("warmup")
    )

    async def market_gate(*, require_ready=False):
        events.append("market_gate")
        assert "bar_consumer" in events
        return True

    async def bar_loop(ready):
        events.append("bar_consumer")
        ready.set()
        await blocker.wait()

    async def idle_loop():
        await blocker.wait()

    process._refresh_market_gate = market_gate
    process._bar_loop = bar_loop
    process._kline_loop = idle_loop
    process._safety_scan_loop = idle_loop
    process._unregister_market_subscriptions = AsyncMock()

    try:
        await process.start()
    finally:
        await process.stop()

    assert events.index("registered") < events.index("bar_consumer")
    assert events.index("bar_consumer") < events.index("market_gate")


@pytest.mark.asyncio
async def test_local_bar_delivery_gate_requires_every_symbol_and_expires():
    settings = SpikeLiveSettings(
        account_id="spike-test",
        symbols=["AKEUSDT", "BTCUSDT"],
        total_notional="20",
    )
    process = SpikeLiveProcess(
        settings,
        binance=Mock(),
        database=Mock(),
        redis_config=Mock(),
        strategy_config=Mock(account_id="spike-test"),
    )
    strategy = StrategyStub()
    process.gate = CompositeEntryGate(strategy)

    process._last_bar_received_monotonic = {"AKEUSDT": 100.0}
    assert process._refresh_bar_stream_gate(now=100.0) is False
    assert process.gate.condition("bar_stream") is False

    process._last_bar_received_monotonic["BTCUSDT"] = 95.0
    assert process._refresh_bar_stream_gate(now=100.0) is True
    assert process.gate.condition("bar_stream") is True

    assert process._refresh_bar_stream_gate(now=110.001) is False
    assert process.gate.condition("bar_stream") is False


@pytest.mark.asyncio
async def test_runtime_callbacks_wait_for_startup_recovery_barrier():
    delegate = Mock(
        handle_execution_report=AsyncMock(),
        handle_account_update=AsyncMock(),
    )
    account = Mock(handle_execution_report=Mock(return_value=None))
    coordinator = Mock(
        reconcile_entry_expirations=AsyncMock(),
        maybe_release_campaign=AsyncMock(),
    )
    callbacks = SpikeRuntimeCallbacks(
        delegate=delegate,
        account=account,
        coordinator=coordinator,
        gate=Mock(),
    )
    callbacks.begin_startup_recovery()
    task = asyncio.create_task(callbacks.handle_execution_report({"s": "AKEUSDT"}))
    await asyncio.sleep(0)

    delegate.handle_execution_report.assert_not_awaited()
    callbacks.finish_startup_recovery()
    await task
    delegate.handle_execution_report.assert_awaited_once()


@pytest.mark.asyncio
async def test_kline_loop_consumes_15m_updates():
    settings = SpikeLiveSettings(
        account_id="spike-test", symbols=["AKEUSDT"], total_notional="10"
    )
    process = SpikeLiveProcess(
        settings,
        binance=Mock(),
        database=Mock(),
        redis_config=Mock(),
        strategy_config=Mock(account_id="spike-test"),
    )
    seen_keys = []

    async def hget(key, field):
        seen_keys.append(key)
        if key.endswith(":15m"):
            raise asyncio.CancelledError
        return None

    process.redis = Mock(hget=AsyncMock(side_effect=hget))
    process.coordinator = Mock()

    with pytest.raises(asyncio.CancelledError):
        await process._kline_loop()

    assert "kline:AKEUSDT:15m" in seen_keys


def test_composite_gate_requires_every_condition():
    strategy = StrategyStub()
    gate = CompositeEntryGate(strategy)
    gate.set_condition("execution", True)
    gate.set_condition("market", False)
    assert gate.enabled is False
    assert strategy.enabled is False
    gate.set_condition("market", True)
    assert gate.enabled is True
    assert strategy.enabled is True
    assert gate.condition("market") is True
    assert gate.condition("missing") is False


@pytest.mark.asyncio
async def test_missing_redis_campaign_with_live_risk_halts_instead_of_opening_gate():
    strategy = StrategyStub()
    gate = CompositeEntryGate(strategy)
    risk = RiskGuard("spike-test", RiskConfig())
    coordinator = SpikeExecutionCoordinator(
        strategy=strategy,
        account=Mock(symbols_with_live_risk=Mock(return_value={"BTCUSDT"})),
        executor=Mock(),
        campaign_store=Mock(get_active=AsyncMock(return_value=None)),
        risk_guard=risk,
        gate=gate,
        account_id="spike-test",
    )

    with pytest.raises(RuntimeError, match="disappeared"):
        await coordinator.restore_campaign_gate()

    assert risk.halted is True
    assert gate.condition("campaign") is False


@pytest.mark.asyncio
async def test_reduce_only_exit_waits_while_execution_stream_is_unavailable():
    strategy = StrategyStub()
    gate = CompositeEntryGate(strategy)
    gate.set_condition("execution", False)
    risk = RiskGuard("spike-test", RiskConfig())
    executor = Mock(submit=AsyncMock())
    coordinator = SpikeExecutionCoordinator(
        strategy=strategy,
        account=Mock(),
        executor=executor,
        campaign_store=Mock(),
        risk_guard=risk,
        gate=gate,
        account_id="spike-test",
    )
    exit_intent = OrderIntent(
        symbol="BTCUSDT",
        side="BUY",
        price=Decimal("99"),
        quantity=Decimal("1"),
        client_order_id="blocked-exit",
        order_type="MARKET",
        reduce_only=True,
        strategy_id="spike_short",
        trigger_reason="candidate_momentum_exit",
    )

    await coordinator._execute([exit_intent], event_time=1_001)

    executor.submit.assert_not_awaited()
    assert risk.halted is True


@pytest.mark.asyncio
async def test_order_submission_requires_explicit_owned_campaign():
    strategy = StrategyStub()
    gate = CompositeEntryGate(strategy)
    executor = Mock(submit=AsyncMock())
    coordinator = SpikeExecutionCoordinator(
        strategy=strategy,
        account=Mock(),
        executor=executor,
        campaign_store=Mock(),
        risk_guard=RiskGuard("spike-test", RiskConfig()),
        gate=gate,
        account_id="spike-test",
    )

    with pytest.raises(RuntimeError, match="without an owned Campaign"):
        await coordinator._submit(_entry())

    executor.submit.assert_not_awaited()
    assert gate.condition("campaign") is False


def test_stream_disconnect_closes_execution_gate_immediately():
    settings = SpikeLiveSettings(
        account_id="spike-test", symbols=["AKEUSDT"], total_notional="20"
    )
    process = SpikeLiveProcess(
        settings,
        binance=Mock(),
        database=Mock(),
        redis_config=Mock(),
        strategy_config=Mock(account_id="spike-test"),
    )
    process.gate = Mock(set_condition=Mock())

    process._on_execution_stream_disconnected()

    process.gate.set_condition.assert_called_once_with("execution", False)


@pytest.mark.asyncio
async def test_stream_callback_failure_halts_process_fail_closed():
    settings = SpikeLiveSettings(
        account_id="spike-test", symbols=["AKEUSDT"], total_notional="20"
    )
    process = SpikeLiveProcess(
        settings,
        binance=Mock(),
        database=Mock(),
        redis_config=Mock(),
        strategy_config=Mock(account_id="spike-test"),
    )
    failure = RuntimeError("postgres unavailable")
    process.runtime = Mock(
        user_stream=Mock(wait_fatal=AsyncMock(return_value=failure))
    )
    process.gate = Mock(set_condition=Mock())
    risk = Mock(halt=Mock())
    process.coordinator = Mock(risk_guard=risk)

    with pytest.raises(RuntimeError, match="execution stream callback failed"):
        await process._execution_stream_fatal_loop()

    process.gate.set_condition.assert_called_once_with("execution", False)
    risk.halt.assert_called_once_with(
        "execution stream callback failed: RuntimeError"
    )


@pytest.mark.asyncio
async def test_submit_unknown_attempt_exhaustion_halts_process_fail_closed():
    settings = SpikeLiveSettings(
        account_id="spike-test", symbols=["AKEUSDT"], total_notional="20"
    )
    process = SpikeLiveProcess(
        settings,
        binance=Mock(),
        database=Mock(),
        redis_config=Mock(),
        strategy_config=Mock(account_id="spike-test"),
    )
    failure = RuntimeError("SUBMIT_UNKNOWN resolution attempts exhausted")
    process.runtime = Mock(
        unknown_poller=Mock(wait_fatal=AsyncMock(return_value=failure))
    )
    process.gate = Mock(set_condition=Mock())
    risk = Mock(halt=Mock())
    process.coordinator = Mock(risk_guard=risk)

    with pytest.raises(RuntimeError, match="SUBMIT_UNKNOWN recovery failed"):
        await process._submit_unknown_fatal_loop()

    process.gate.set_condition.assert_called_once_with("execution", False)
    risk.halt.assert_called_once_with(
        "SUBMIT_UNKNOWN resolution attempts exhausted"
    )


@pytest.mark.asyncio
async def test_execution_lease_loss_halts_process_fail_closed():
    settings = SpikeLiveSettings(
        account_id="spike-test", symbols=["AKEUSDT"], total_notional="20"
    )
    process = SpikeLiveProcess(
        settings,
        binance=Mock(),
        database=Mock(),
        redis_config=Mock(),
        strategy_config=Mock(account_id="spike-test"),
    )
    failure = ConnectionError("postgres session lost")
    process.execution_lease = Mock(wait_lost=AsyncMock(return_value=failure))
    process.gate = Mock(set_condition=Mock())
    risk = Mock(halt=Mock())
    process.coordinator = Mock(risk_guard=risk)

    with pytest.raises(RuntimeError, match="execution account lease lost"):
        await process._execution_lease_fatal_loop()

    process.gate.set_condition.assert_called_once_with("execution", False)
    risk.halt.assert_called_once_with(
        "execution account lease lost: ConnectionError"
    )


@pytest.mark.asyncio
async def test_fatal_status_write_failure_does_not_mask_stream_failure():
    settings = SpikeLiveSettings(
        account_id="spike-test", symbols=["AKEUSDT"], total_notional="20"
    )
    process = SpikeLiveProcess(
        settings,
        binance=Mock(),
        database=Mock(),
        redis_config=Mock(),
        strategy_config=Mock(account_id="spike-test"),
    )
    process.runtime = Mock(
        user_stream=Mock(wait_fatal=AsyncMock(return_value=ValueError("bad report")))
    )
    process.gate = Mock(set_condition=Mock())
    risk = Mock(halt=Mock(), halted=True, halt_reason="callback failed")
    process.coordinator = Mock(risk_guard=risk)
    process.db = Mock(
        upsert_strategy_runtime_status=AsyncMock(
            side_effect=ConnectionError("postgres unavailable")
        )
    )

    with pytest.raises(RuntimeError, match="execution stream callback failed"):
        await process._execution_stream_fatal_loop()

    process.gate.set_condition.assert_called_once_with("execution", False)
    risk.halt.assert_called_once_with(
        "execution stream callback failed: ValueError"
    )


@pytest.mark.asyncio
async def test_runtime_heartbeat_failure_halts_process_fail_closed(monkeypatch):
    settings = SpikeLiveSettings(
        account_id="spike-test", symbols=["AKEUSDT"], total_notional="20"
    )
    process = SpikeLiveProcess(
        settings,
        binance=Mock(),
        database=Mock(),
        redis_config=Mock(),
        strategy_config=Mock(account_id="spike-test"),
    )
    process.gate = Mock(set_condition=Mock())
    risk = Mock(halt=Mock())
    process.coordinator = Mock(risk_guard=risk)
    process._publish_runtime_status = AsyncMock(
        side_effect=ConnectionError("postgres unavailable")
    )
    monkeypatch.setattr(
        "trading_platform.strategies.spike_main.asyncio.sleep", AsyncMock()
    )

    with pytest.raises(RuntimeError, match="runtime status heartbeat failed"):
        await process._runtime_heartbeat_loop()

    process.gate.set_condition.assert_called_once_with("execution", False)
    risk.halt.assert_called_once_with(
        "runtime status heartbeat failed: ConnectionError"
    )


@pytest.mark.asyncio
async def test_runtime_status_ownership_loss_halts_execution():
    settings = SpikeLiveSettings(
        account_id="spike-test", symbols=["AKEUSDT"], total_notional="20"
    )
    process = SpikeLiveProcess(
        settings,
        binance=Mock(),
        database=Mock(),
        redis_config=Mock(),
        strategy_config=Mock(account_id="spike-test"),
    )
    strategy = StrategyStub()
    process.gate = CompositeEntryGate(strategy)
    process.gate.set_condition("execution", True)
    risk = RiskGuard("spike-test", RiskConfig())
    process.coordinator = Mock(risk_guard=risk)
    process.db = Mock(upsert_strategy_runtime_status=AsyncMock(return_value=False))

    with pytest.raises(RuntimeError, match="runtime status ownership lost"):
        await process._publish_runtime_status()

    assert process.gate.condition("execution") is False
    assert risk.halted is True


def test_stream_recovery_reopens_execution_only_when_all_facts_are_safe():
    settings = SpikeLiveSettings(
        account_id="spike-test", symbols=["AKEUSDT"], total_notional="20"
    )
    process = SpikeLiveProcess(
        settings,
        binance=Mock(),
        database=Mock(),
        redis_config=Mock(),
        strategy_config=Mock(account_id="spike-test"),
    )
    gate = Mock(set_condition=Mock())
    risk = Mock(halted=False)
    account = Mock(has_unresolved_orders=Mock(return_value=False))
    process.gate = gate
    process.coordinator = Mock(risk_guard=risk, account=account)
    process.runtime = Mock(user_stream=Mock(connected=True))

    assert process._restore_execution_gate() is True
    gate.set_condition.assert_called_with("execution", True)

    gate.reset_mock()
    account.has_unresolved_orders.return_value = True
    assert process._restore_execution_gate() is False
    gate.set_condition.assert_called_with("execution", False)

    gate.reset_mock()
    account.has_unresolved_orders.return_value = False
    risk.halted = True
    assert process._restore_execution_gate() is False
    gate.set_condition.assert_called_with("execution", False)

    gate.reset_mock()
    risk.halted = False
    process.execution_lease = Mock(held=False)
    assert process._restore_execution_gate() is False
    gate.set_condition.assert_called_with("execution", False)


def test_disconnect_recovery_sequence_waits_for_resolved_orders_before_reopening():
    settings = SpikeLiveSettings(
        account_id="spike-test", symbols=["AKEUSDT"], total_notional="20"
    )
    process = SpikeLiveProcess(
        settings,
        binance=Mock(),
        database=Mock(),
        redis_config=Mock(),
        strategy_config=Mock(account_id="spike-test"),
    )
    gate = Mock(set_condition=Mock())
    risk = Mock(halted=False)
    account = Mock(has_unresolved_orders=Mock(return_value=True))
    stream = Mock(connected=False)
    process.gate = gate
    process.coordinator = Mock(risk_guard=risk, account=account)
    process.runtime = Mock(user_stream=stream)

    process._on_execution_stream_disconnected()
    stream.connected = True
    assert process._restore_execution_gate() is False
    account.has_unresolved_orders.return_value = False
    assert process._restore_execution_gate() is True

    assert gate.set_condition.call_args_list == [
        (("execution", False),),
        (("execution", False),),
        (("execution", True),),
    ]


@pytest.mark.asyncio
async def test_entry_acquires_campaign_then_submits_and_exit_is_reduce_only():
    strategy = StrategyStub()
    gate = CompositeEntryGate(strategy)
    for name in ("execution", "market", "subcategory", "campaign"):
        gate.set_condition(name, True)
    account = Mock(
        has_open_position=Mock(return_value=False),
        all_orders_terminal=Mock(return_value=False),
        flush_cancellations=AsyncMock(return_value=()),
        has_pending_cancellations=False,
    )
    executor = Mock(submit=AsyncMock(return_value=Mock(status="NEW")))
    store = Mock(
        get_active=AsyncMock(return_value=None),
        acquire=AsyncMock(return_value=True),
        release=AsyncMock(return_value=False),
    )
    coordinator = SpikeExecutionCoordinator(
        strategy=strategy,
        account=account,
        executor=executor,
        campaign_store=store,
        risk_guard=RiskGuard("spike-test", RiskConfig()),
        gate=gate,
        account_id="spike-test",
    )
    exit_intent = OrderIntent(
        symbol="BTCUSDT",
        side="BUY",
        price=Decimal("99"),
        quantity=Decimal("1"),
        client_order_id="exit-1",
        order_type="MARKET",
        reduce_only=True,
        strategy_id="spike_short",
        trigger_reason="campaign_timeout_exit",
    )

    await coordinator._execute([_entry(), exit_intent], event_time=1_001)

    store.acquire.assert_awaited_once()
    assert executor.submit.await_args_list[0].args[0].campaign_id == (
        "spike_short:BTCUSDT:1000"
    )
    assert executor.submit.await_args_list[1].args[0].campaign_id == (
        "spike_short:BTCUSDT:1000"
    )
    assert executor.submit.await_args_list[0].kwargs == {
        "reference_price": Decimal("100"),
    }
    assert executor.submit.await_args_list[1].kwargs == {
        "reference_price": Decimal("99"),
    }


@pytest.mark.asyncio
async def test_candidate_exit_is_submitted_before_redis_state_is_persisted():
    events = []
    exit_intent = OrderIntent(
        symbol="BTCUSDT",
        side="BUY",
        price=Decimal("99"),
        quantity=Decimal("1"),
        client_order_id="candidate-exit-1",
        order_type="MARKET",
        reduce_only=True,
        strategy_id="spike_short",
        trigger_reason="candidate_time_risk_exit",
    )
    strategy = StrategyStub()
    strategy.on_bar1s = Mock(return_value=[exit_intent])
    strategy.campaign_exit_state = Mock(return_value=(False, False, True))
    gate = CompositeEntryGate(strategy)
    gate.set_condition("execution", True)
    account = Mock(
        flush_cancellations=AsyncMock(return_value=()),
        has_pending_cancellations=False,
        has_open_position=Mock(return_value=True),
    )

    async def submit(*args, **kwargs):
        events.append("submit")
        return Mock(status="NEW")

    async def persist(*args, **kwargs):
        events.append("persist")
        return True

    store = Mock(update_exit_state=AsyncMock(side_effect=persist))
    coordinator = SpikeExecutionCoordinator(
        strategy=strategy,
        account=account,
        executor=Mock(submit=AsyncMock(side_effect=submit)),
        campaign_store=store,
        risk_guard=RiskGuard("spike-test", RiskConfig()),
        gate=gate,
        account_id="spike-test",
    )
    coordinator._owned_campaign_id = "spike_short:BTCUSDT:1000"
    coordinator._owned_campaign_lease = CampaignLease(
        "spike_short:BTCUSDT:1000", "spike_short", "BTCUSDT", 1_000
    )
    bar = Bar1s(
        symbol="BTCUSDT",
        timestamp=2_000,
        available_time=2_000,
        open=Decimal("99"),
        high=Decimal("99"),
        low=Decimal("99"),
        close=Decimal("99"),
        volume=Decimal("1"),
        trade_count=1,
        vwap=Decimal("99"),
    )

    await coordinator.on_bar1s(bar)

    assert events == ["submit", "persist"]
    store.update_exit_state.assert_awaited_once_with(
        "spike_short:BTCUSDT:1000",
        origin_checked=False,
        reduced_at_origin=False,
        exit_requested=True,
    )


@pytest.mark.asyncio
async def test_halted_risk_rejects_entry_but_still_submits_reduce_only_exit():
    strategy = StrategyStub()
    gate = CompositeEntryGate(strategy)
    for name in ("execution", "market", "subcategory", "campaign"):
        gate.set_condition(name, True)
    risk = RiskGuard("spike-test", RiskConfig())
    risk.halt("execution fact unknown")
    executor = Mock(submit=AsyncMock(return_value=Mock(status="NEW")))
    coordinator = SpikeExecutionCoordinator(
        strategy=strategy,
        account=Mock(),
        executor=executor,
        campaign_store=Mock(
            get_active=AsyncMock(return_value=None),
            acquire=AsyncMock(return_value=True),
        ),
        risk_guard=risk,
        gate=gate,
        account_id="spike-test",
    )
    exit_intent = OrderIntent(
        symbol="BTCUSDT",
        side="BUY",
        price=Decimal("99"),
        quantity=Decimal("1"),
        client_order_id="exit-after-halt",
        order_type="MARKET",
        reduce_only=True,
        strategy_id="spike_short",
        trigger_reason="campaign_timeout_exit",
    )

    await coordinator._execute([_entry(), exit_intent], event_time=1_001)

    executor.submit.assert_awaited_once_with(
        replace(exit_intent, campaign_id="spike_short:BTCUSDT:1000"),
        reference_price=Decimal("99"),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("has_position", "orders_terminal", "expected"),
    [
        (True, True, False),
        (False, False, False),
        (False, True, True),
    ],
)
async def test_campaign_release_requires_flat_position_and_terminal_orders(
    has_position, orders_terminal, expected
):
    strategy = StrategyStub()
    gate = CompositeEntryGate(strategy)
    gate.set_condition("campaign", False)
    store = Mock(release=AsyncMock(return_value=True))
    coordinator = SpikeExecutionCoordinator(
        strategy=strategy,
        account=Mock(
            has_open_position=Mock(return_value=has_position),
            has_pending_position_update=Mock(return_value=False),
            all_orders_terminal=Mock(return_value=orders_terminal),
        ),
        executor=Mock(),
        campaign_store=store,
        risk_guard=RiskGuard("spike-test", RiskConfig()),
        gate=gate,
        account_id="spike-test",
    )
    coordinator._owned_campaign_id = "spike_short:BTCUSDT:1000"

    assert await coordinator.maybe_release_campaign("BTCUSDT") is expected

    if expected:
        store.release.assert_awaited_once_with("spike_short:BTCUSDT:1000")
        assert coordinator._owned_campaign_id is None
        assert gate.enabled is True
    else:
        store.release.assert_not_awaited()
        assert coordinator._owned_campaign_id == "spike_short:BTCUSDT:1000"
        assert gate.enabled is False


@pytest.mark.asyncio
async def test_failed_campaign_release_keeps_lease_owned_and_gate_closed():
    strategy = StrategyStub()
    gate = CompositeEntryGate(strategy)
    gate.set_condition("campaign", False)
    store = Mock(release=AsyncMock(return_value=False))
    coordinator = SpikeExecutionCoordinator(
        strategy=strategy,
        account=Mock(
            has_open_position=Mock(return_value=False),
            has_pending_position_update=Mock(return_value=False),
            all_orders_terminal=Mock(return_value=True),
        ),
        executor=Mock(),
        campaign_store=store,
        risk_guard=RiskGuard("spike-test", RiskConfig()),
        gate=gate,
        account_id="spike-test",
    )
    coordinator._owned_campaign_id = "spike_short:BTCUSDT:1000"

    assert await coordinator.maybe_release_campaign("BTCUSDT") is False
    assert coordinator._owned_campaign_id == "spike_short:BTCUSDT:1000"
    assert gate.enabled is False


@pytest.mark.asyncio
async def test_trade_fill_blocks_campaign_release_until_account_update(tmp_path):
    rest = Mock(get_position_risk=AsyncMock(return_value=[]))
    risk = RiskGuard("spike-test", RiskConfig())
    wal = OrderWAL(tmp_path / "orders.jsonl")
    account = BinanceStrategyAccount(
        rest,
        wal,
        account_id="spike-test",
        strategy_id="spike_short",
        risk_guard=risk,
    )
    intent = _entry()
    intent_record = wal.record_intent(intent, account_id="spike-test", recorded_at=1_000)
    wal.record_exchange_status(
        intent_record, {"status": "FILLED", "orderId": 42}, recorded_at=1_100
    )
    account.handle_execution_report(
        {
            "c": intent.client_order_id,
            "x": "TRADE",
            "X": "FILLED",
            "l": "1",
            "L": "100",
            "t": 7,
            "T": 2_000,
        }
    )
    strategy = StrategyStub()
    gate = CompositeEntryGate(strategy)
    gate.set_condition("campaign", False)
    store = Mock(release=AsyncMock(return_value=True))
    coordinator = SpikeExecutionCoordinator(
        strategy=strategy,
        account=account,
        executor=Mock(),
        campaign_store=store,
        risk_guard=risk,
        gate=gate,
        account_id="spike-test",
    )
    coordinator._owned_campaign_id = "spike_short:BTCUSDT:1000"

    assert await coordinator.maybe_release_campaign("BTCUSDT") is False
    store.release.assert_not_awaited()

    await account.handle_account_update(
        {
            "e": "ACCOUNT_UPDATE",
            "T": 2_000,
            "a": {
                "P": [
                    {
                        "s": "BTCUSDT",
                        "pa": "-1",
                        "ep": "100",
                        "up": "0",
                        "ps": "BOTH",
                    }
                ]
            },
        }
    )

    assert account.has_pending_position_update("BTCUSDT") is False
    assert await coordinator.maybe_release_campaign("BTCUSDT") is False
    store.release.assert_not_awaited()


@pytest.mark.asyncio
async def test_foreign_campaign_prevents_entry_submission():
    strategy = StrategyStub()
    gate = CompositeEntryGate(strategy)
    gate.set_condition("execution", True)
    gate.set_condition("market", True)
    gate.set_condition("subcategory", True)
    gate.set_condition("campaign", True)
    store = Mock(
        get_active=AsyncMock(
            return_value=Mock(campaign_id="other:ETHUSDT:1", strategy_id="other")
        ),
        acquire=AsyncMock(),
    )
    executor = Mock(submit=AsyncMock())
    coordinator = SpikeExecutionCoordinator(
        strategy=strategy,
        account=Mock(),
        executor=executor,
        campaign_store=store,
        risk_guard=RiskGuard("spike-test", RiskConfig()),
        gate=gate,
        account_id="spike-test",
    )

    await coordinator._execute([_entry()], event_time=1_001)

    executor.submit.assert_not_awaited()
    assert gate.enabled is False


@pytest.mark.asyncio
async def test_submit_unknown_halts_risk_guard_and_closes_execution_gate():
    strategy = StrategyStub()
    gate = CompositeEntryGate(strategy)
    for name in ("execution", "market", "subcategory", "campaign"):
        gate.set_condition(name, True)
    risk = RiskGuard("spike-test", RiskConfig())
    account = Mock(has_pending_cancellations=False)
    executor = Mock(submit=AsyncMock(return_value=Mock(status="SUBMIT_UNKNOWN")))
    store = Mock(
        get_active=AsyncMock(return_value=None),
        acquire=AsyncMock(return_value=True),
    )
    coordinator = SpikeExecutionCoordinator(
        strategy=strategy,
        account=account,
        executor=executor,
        campaign_store=store,
        risk_guard=risk,
        gate=gate,
        account_id="spike-test",
    )

    await coordinator._execute([_entry()], event_time=1_001)

    assert risk.halted is True
    assert risk.halt_reason == "submit status unknown"
    assert gate.enabled is False


@pytest.mark.asyncio
async def test_exchange_rejection_halts_and_aborts_execution():
    strategy = StrategyStub()
    gate = CompositeEntryGate(strategy)
    for name in ("execution", "market", "subcategory", "campaign"):
        gate.set_condition(name, True)
    risk = RiskGuard("spike-test", RiskConfig())
    executor = Mock(
        submit=AsyncMock(
            return_value=Mock(
                status="REJECTED",
                client_order_id="cid-rejected",
                payload={"exchange_response": {"code": -2019}},
            )
        )
    )
    coordinator = SpikeExecutionCoordinator(
        strategy=strategy,
        account=Mock(has_pending_cancellations=False),
        executor=executor,
        campaign_store=Mock(
            get_active=AsyncMock(return_value=None),
            acquire=AsyncMock(return_value=True),
        ),
        risk_guard=risk,
        gate=gate,
        account_id="spike-test",
    )

    with pytest.raises(RuntimeError, match="exchange rejected order cid-rejected"):
        await coordinator._execute([_entry()], event_time=1_001)

    assert risk.halted is True
    assert risk.halt_reason == "order rejected by exchange: -2019"
    assert gate.enabled is False


@pytest.mark.asyncio
async def test_pending_cancellation_halts_risk_guard_and_closes_execution_gate():
    strategy = StrategyStub()
    gate = CompositeEntryGate(strategy)
    gate.set_condition("execution", True)
    risk = RiskGuard("spike-test", RiskConfig())
    account = Mock(
        flush_cancellations=AsyncMock(return_value=()),
        has_pending_cancellations=True,
    )
    coordinator = SpikeExecutionCoordinator(
        strategy=strategy,
        account=account,
        executor=Mock(),
        campaign_store=Mock(),
        risk_guard=risk,
        gate=gate,
        account_id="spike-test",
    )

    await coordinator._flush_cancellations()

    assert risk.halted is True
    assert risk.halt_reason == "entry cancellation unresolved"
    assert gate.enabled is False


@pytest.mark.asyncio
async def test_completed_entry_cancellation_refreshes_position_before_next_exit():
    strategy = StrategyStub()
    account = Mock(
        flush_cancellations=AsyncMock(return_value=("entry-1",)),
        has_pending_cancellations=False,
        refresh_positions=AsyncMock(),
    )
    coordinator = SpikeExecutionCoordinator(
        strategy=strategy,
        account=account,
        executor=Mock(),
        campaign_store=Mock(),
        risk_guard=RiskGuard("spike-test", RiskConfig()),
        gate=CompositeEntryGate(strategy),
        account_id="spike-test",
    )

    await coordinator._flush_cancellations()

    account.refresh_positions.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_shutdown_cancels_every_open_entry_order():
    strategy = StrategyStub()
    gate = CompositeEntryGate(strategy)
    open_order = Mock(
        order_id="11",
        client_order_id="entry-1",
        trigger_reason="spike_tier1",
        reduce_only=False,
        status="NEW",
    )
    closed_order = Mock(
        order_id="11",
        client_order_id="entry-1",
        trigger_reason="spike_tier1",
        reduce_only=False,
        status="CANCELLED",
    )
    account = Mock(
        iter_orders=Mock(side_effect=[(open_order,), (closed_order,)]),
        cancel_order=Mock(return_value=True),
        flush_cancellations=AsyncMock(return_value=("entry-1",)),
        has_pending_cancellations=False,
        refresh_positions=AsyncMock(),
    )
    coordinator = SpikeExecutionCoordinator(
        strategy=strategy,
        account=account,
        executor=Mock(),
        campaign_store=Mock(),
        risk_guard=RiskGuard("spike-test", RiskConfig()),
        gate=gate,
        account_id="spike-test",
    )

    await coordinator.stop()

    account.cancel_order.assert_called_once_with("11")
    account.flush_cancellations.assert_awaited_once()


@pytest.mark.asyncio
async def test_restart_immediately_cancels_entry_whose_wal_ttl_elapsed():
    strategy = StrategyStub()
    gate = CompositeEntryGate(strategy)
    expired = Mock(
        order_id="11",
        client_order_id="entry-expired",
        trigger_reason="spike_tier1",
        reduce_only=False,
        status="NEW",
        created_at=1_000,
        ttl_ms=100,
    )
    account = Mock(
        iter_orders=Mock(return_value=(expired,)),
        cancel_order=Mock(return_value=True),
        flush_cancellations=AsyncMock(return_value=("entry-expired",)),
        has_pending_cancellations=False,
        refresh_positions=AsyncMock(),
    )
    coordinator = SpikeExecutionCoordinator(
        strategy=strategy,
        account=account,
        executor=Mock(),
        campaign_store=Mock(),
        risk_guard=RiskGuard("spike-test", RiskConfig()),
        gate=gate,
        account_id="spike-test",
        now_ms=lambda: 1_200,
    )

    await coordinator.reconcile_entry_expirations()

    account.cancel_order.assert_called_once_with("11")
    account.flush_cancellations.assert_awaited_once()


def test_recovered_live_risk_without_owned_campaign_fails_closed():
    strategy = StrategyStub()
    gate = CompositeEntryGate(strategy)
    gate.set_condition("campaign", True)
    coordinator = SpikeExecutionCoordinator(
        strategy=strategy,
        account=Mock(symbols_with_live_risk=Mock(return_value={"BTCUSDT"})),
        executor=Mock(),
        campaign_store=Mock(),
        risk_guard=RiskGuard("spike-test", RiskConfig()),
        gate=gate,
        account_id="spike-test",
    )

    with pytest.raises(RuntimeError, match="without an owned Redis Campaign"):
        coordinator.validate_recovered_campaign()

    assert gate.enabled is False


def test_recovered_campaign_must_cover_only_its_symbol():
    strategy = StrategyStub()
    gate = CompositeEntryGate(strategy)
    coordinator = SpikeExecutionCoordinator(
        strategy=strategy,
        account=Mock(
            symbols_with_live_risk=Mock(return_value={"BTCUSDT", "ETHUSDT"})
        ),
        executor=Mock(),
        campaign_store=Mock(),
        risk_guard=RiskGuard("spike-test", RiskConfig()),
        gate=gate,
        account_id="spike-test",
    )
    coordinator._owned_campaign_id = "spike_short:BTCUSDT:1000"

    with pytest.raises(RuntimeError, match="multiple symbols"):
        coordinator.validate_recovered_campaign()


@pytest.mark.asyncio
async def test_callback_failure_closes_execution_gate():
    strategy = StrategyStub()
    gate = CompositeEntryGate(strategy)
    gate.set_condition("execution", True)
    delegate = Mock(
        handle_execution_report=AsyncMock(side_effect=RuntimeError("db unavailable")),
        handle_account_update=AsyncMock(),
    )
    callbacks = SpikeRuntimeCallbacks(
        delegate=delegate,
        account=Mock(),
        coordinator=Mock(),
        gate=gate,
    )

    with pytest.raises(RuntimeError, match="db unavailable"):
        await callbacks.handle_execution_report({"s": "BTCUSDT"})

    assert gate.enabled is False


@pytest.mark.asyncio
async def test_execution_report_callback_failure_halts_risk_guard():
    strategy = StrategyStub()
    gate = CompositeEntryGate(strategy)
    gate.set_condition("execution", True)
    risk = RiskGuard("spike-test", RiskConfig())
    delegate = Mock(
        handle_execution_report=AsyncMock(side_effect=RuntimeError("wal unavailable")),
        handle_account_update=AsyncMock(),
    )
    callbacks = SpikeRuntimeCallbacks(
        delegate=delegate,
        account=Mock(has_pending_cancellations=False),
        coordinator=Mock(),
        gate=gate,
        risk_guard=risk,
    )

    with pytest.raises(RuntimeError, match="wal unavailable"):
        await callbacks.handle_execution_report({"s": "BTCUSDT"})

    assert risk.halted is True
    assert risk.halt_reason == "execution report handling failed"
    assert gate.enabled is False


@pytest.mark.asyncio
async def test_account_update_callback_failure_halts_risk_guard():
    strategy = StrategyStub()
    gate = CompositeEntryGate(strategy)
    gate.set_condition("execution", True)
    risk = RiskGuard("spike-test", RiskConfig())
    delegate = Mock(
        handle_execution_report=AsyncMock(),
        handle_account_update=AsyncMock(side_effect=RuntimeError("ledger unavailable")),
    )
    callbacks = SpikeRuntimeCallbacks(
        delegate=delegate,
        account=Mock(has_pending_cancellations=False),
        coordinator=Mock(),
        gate=gate,
        risk_guard=risk,
    )

    with pytest.raises(RuntimeError, match="ledger unavailable"):
        await callbacks.handle_account_update({"a": {"P": []}})

    assert risk.halted is True
    assert risk.halt_reason == "account update handling failed"
    assert gate.enabled is False
