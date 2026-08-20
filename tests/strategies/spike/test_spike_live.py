import asyncio
from dataclasses import replace
from decimal import Decimal
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError

from trading_platform.shared.binance.strategy_account import BinanceStrategyAccount
from trading_platform.shared.events import Bar1s, OrderIntent, StrategyAuditEvent
from trading_platform.shared.execution_recovery import (
    OrderWAL,
    Resolution,
    SubmitUnknownPollingService,
)
from trading_platform.shared.risk import RiskConfig, RiskGuard
from trading_platform.strategies.spike.live import (
    LIVE_CONFIRMATION,
    CompositeEntryGate,
    SpikeExecutionCoordinator,
    SpikeLiveSettings,
    SpikeRuntimeCallbacks,
    require_one_way_position_mode,
)
from trading_platform.strategies.campaign_store import CampaignLease
from trading_platform.strategies.spike.main import (
    EXCHANGE_RULE_REFRESH_INTERVAL_SECONDS,
    SpikeLiveProcess,
    _snapshot_from_database,
    require_viable_entry_notional,
)
from trading_platform.strategies.universe import ExchangeSymbolSnapshot


class StrategyStub:
    def __init__(self):
        self.enabled = None
        self.strategies = {"BTCUSDT": object()}
        self.blocked_entry_symbols = frozenset()

    def set_entry_enabled(self, enabled):
        self.enabled = enabled

    def set_blocked_entry_symbols(self, symbols):
        self.blocked_entry_symbols = frozenset(symbols)

    def is_symbol_entry_enabled(self, symbol):
        return symbol not in self.blocked_entry_symbols

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


def test_database_admission_builds_managed_symbol_snapshot():
    restricted = _snapshot_from_database(
        ["AKEUSDT", "BTCUSDT", "OLDUSDT"], ["BTCUSDT"]
    )

    assert restricted.allowed_symbols == frozenset({"BTCUSDT"})
    assert restricted.blocked_symbols == frozenset({"AKEUSDT", "OLDUSDT"})
    assert restricted.blocked_reasons["AKEUSDT"] == "database_admission"


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


def _watermarked_bar(trade_id: int, *, timestamp: int | None = None) -> Bar1s:
    event_time = trade_id * 1_000 if timestamp is None else timestamp
    return Bar1s(
        symbol="BTCUSDT",
        timestamp=event_time,
        available_time=event_time + 1_000,
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100"),
        volume=Decimal("1"),
        trade_count=1,
        vwap=Decimal("100"),
        first_aggregate_trade_id=trade_id,
        last_aggregate_trade_id=trade_id,
    )


def _continuity_process() -> SpikeLiveProcess:
    process = SpikeLiveProcess(
        SpikeLiveSettings(
            account_id="spike-test", symbols=["BTCUSDT"], total_notional="20"
        ),
        binance=Mock(),
        database=Mock(),
        redis_config=Mock(),
        strategy_config=Mock(account_id="spike-test"),
    )
    strategy = StrategyStub()
    process.gate = CompositeEntryGate(strategy)
    process.exchange_symbol_snapshot = ExchangeSymbolSnapshot(
        allowed_symbols=frozenset({"BTCUSDT"}),
        blocked_symbols=frozenset(),
        blocked_reasons={},
    )
    process.coordinator = Mock(
        account=Mock(symbols_with_live_risk=Mock(return_value=set())),
        on_bar1s=AsyncMock(),
        cancel_open_entry_orders=AsyncMock(),
    )
    process.redis = AsyncMock()
    process.http = AsyncMock()
    return process


@pytest.mark.asyncio
async def test_bar_continuity_requires_stable_live_bars_and_skips_duplicates():
    process = _continuity_process()
    process.CONTINUITY_STABLE_BARS = 2

    await process._handle_live_bar(_watermarked_bar(10))
    assert process.gate.condition("bar_continuity") is False
    process.coordinator.cancel_open_entry_orders.assert_awaited_once_with()

    await process._handle_live_bar(_watermarked_bar(11))
    assert process.gate.condition("bar_continuity") is True

    await process._handle_live_bar(_watermarked_bar(11))
    assert process.coordinator.on_bar1s.await_count == 2


@pytest.mark.asyncio
async def test_bar_gap_replays_redis_stream_with_entries_closed():
    process = _continuity_process()
    process._last_bar_trade_id["BTCUSDT"] = 10
    process._bar_continuity_streak["BTCUSDT"] = process.CONTINUITY_STABLE_BARS
    process.gate.set_condition("bar_continuity", True)
    recovered = _watermarked_bar(11)
    process.redis.xrevrange = AsyncMock(
        return_value=[("1-0", {"data": recovered.to_json()})]
    )

    await process._handle_live_bar(_watermarked_bar(12))

    assert [call.args[0] for call in process.coordinator.on_bar1s.await_args_list] == [
        recovered,
        _watermarked_bar(12),
    ]
    process.http.get.assert_not_awaited()
    process.coordinator.cancel_open_entry_orders.assert_awaited_once_with()
    assert process.gate.condition("bar_continuity") is False


@pytest.mark.asyncio
async def test_bar_gap_falls_back_to_market_api_when_stream_is_incomplete():
    process = _continuity_process()
    process._last_bar_trade_id["BTCUSDT"] = 20
    process._bar_continuity_streak["BTCUSDT"] = process.CONTINUITY_STABLE_BARS
    process.redis.xrevrange = AsyncMock(return_value=[])
    recovered = _watermarked_bar(21)
    response = Mock()
    response.raise_for_status = Mock()
    response.json.return_value = {"bars": [recovered.to_dict()]}
    process.http.get = AsyncMock(return_value=response)

    await process._handle_live_bar(_watermarked_bar(22))

    process.http.get.assert_awaited_once_with(
        "/bar1s/BTCUSDT/recover", params={"from_id": 21, "to_id": 21}
    )
    assert process.coordinator.on_bar1s.await_args_list[0].args[0] == recovered
    assert process.gate.condition("bar_continuity") is False


@pytest.mark.asyncio
async def test_unrecoverable_bar_gap_stays_closed_but_delivers_current_bar_for_exit():
    process = _continuity_process()
    process._last_bar_trade_id["BTCUSDT"] = 30
    process._bar_continuity_streak["BTCUSDT"] = process.CONTINUITY_STABLE_BARS
    process.redis.xrevrange = AsyncMock(return_value=[])
    response = Mock()
    response.raise_for_status.side_effect = RuntimeError("market unavailable")
    process.http.get = AsyncMock(return_value=response)
    current = _watermarked_bar(32)

    await process._handle_live_bar(current)

    process.coordinator.on_bar1s.assert_awaited_once_with(current)
    process.coordinator.cancel_open_entry_orders.assert_awaited_once_with()
    assert process._last_bar_trade_id["BTCUSDT"] == 32
    assert process.gate.condition("bar_continuity") is False


@pytest.mark.asyncio
async def test_market_epoch_change_closes_entries_and_reregisters_immediately():
    process = _continuity_process()
    process._market_instance_epoch = "old-epoch"
    process._last_bar_trade_id["BTCUSDT"] = 10
    process._bar_continuity_streak["BTCUSDT"] = process.CONTINUITY_STABLE_BARS
    process._register_market_subscriptions = AsyncMock()
    health = Mock()
    health.json.return_value = {
        "status": "ready",
        "instance_epoch": "new-epoch",
        "binance_testnet": True,
    }
    quality = Mock()
    quality.raise_for_status = Mock()
    quality.json.return_value = {"ready": True}
    process.http.get.side_effect = [health, quality]

    assert await process._refresh_market_gate() is True

    assert process._market_instance_epoch == "new-epoch"
    assert process._last_bar_trade_id == {}
    assert process.gate.condition("bar_continuity") is False
    process.coordinator.cancel_open_entry_orders.assert_awaited_once_with()
    process._register_market_subscriptions.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_market_watchdog_cancels_entries_when_bar_stream_is_stale():
    process = _continuity_process()
    process._refresh_market_gate = AsyncMock(return_value=True)
    process._refresh_bar_stream_gate = Mock(return_value=False)

    assert await process._refresh_market_safety_once() is False

    process.coordinator.cancel_open_entry_orders.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_exchange_symbol_admission_cancels_only_blocked_entry_orders():
    strategy = StrategyStub()
    strategy.strategies["HFTUSDT"] = object()
    blocked_entry = Mock(
        order_id="blocked-entry",
        symbol="HFTUSDT",
        reduce_only=False,
        status="NEW",
    )
    blocked_exit = Mock(
        order_id="blocked-exit",
        symbol="HFTUSDT",
        reduce_only=True,
        status="NEW",
    )
    allowed_entry = Mock(
        order_id="allowed-entry",
        symbol="BTCUSDT",
        reduce_only=False,
        status="NEW",
    )
    account = Mock(
        iter_orders=Mock(return_value=(blocked_entry, blocked_exit, allowed_entry)),
        cancel_order=Mock(return_value=True),
        flush_cancellations=AsyncMock(return_value=("blocked-entry",)),
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

    blocked = await coordinator.update_exchange_symbol_admission({"BTCUSDT"})

    assert blocked == frozenset({"HFTUSDT"})
    assert strategy.blocked_entry_symbols == frozenset({"HFTUSDT"})
    account.cancel_order.assert_called_once_with("blocked-entry")
    account.flush_cancellations.assert_awaited_once()


@pytest.mark.asyncio
async def test_market_data_failure_cancels_entries_but_preserves_reduce_only_orders():
    strategy = StrategyStub()
    entry = Mock(
        order_id="entry",
        symbol="BTCUSDT",
        reduce_only=False,
        status="NEW",
    )
    exit_order = Mock(
        order_id="exit",
        symbol="BTCUSDT",
        reduce_only=True,
        status="NEW",
    )
    account = Mock(
        iter_orders=Mock(return_value=(entry, exit_order)),
        cancel_order=Mock(return_value=True),
        flush_cancellations=AsyncMock(return_value=("entry",)),
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

    await coordinator.cancel_open_entry_orders()

    account.cancel_order.assert_called_once_with("entry")
    account.flush_cancellations.assert_awaited_once()


@pytest.mark.asyncio
async def test_blocked_exchange_symbol_cannot_reach_entry_submission():
    strategy = StrategyStub()
    strategy.set_blocked_entry_symbols({"BTCUSDT"})
    gate = CompositeEntryGate(strategy)
    for name in ("execution", "market", "subcategory", "campaign", "exchange_symbols"):
        gate.set_condition(name, True)
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

    await coordinator._execute([_entry()], event_time=1_001)

    executor.submit.assert_not_awaited()


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
        start_execution_worker=Mock(
            side_effect=lambda: asyncio.create_task(blocker.wait())
        ),
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
    process._refresh_exchange_symbol_admission = AsyncMock(return_value=True)

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
async def test_exchange_symbol_refresh_reads_database_without_metadata_request():
    settings = SpikeLiveSettings(
        account_id="spike-test",
        symbols=["AKEUSDT"],
        total_notional="10",
        delisting_freeze_days=15,
    )
    process = SpikeLiveProcess(
        settings,
        binance=Mock(),
        database=Mock(),
        redis_config=Mock(),
        strategy_config=Mock(account_id="spike-test"),
    )
    process._exchange_rules_synced_monotonic = asyncio.get_running_loop().time()
    process.execution_rest = Mock(get_exchange_info=AsyncMock())
    process.db = Mock(
        list_tradeable_exchange_symbols=AsyncMock(return_value=["AKEUSDT"]),
    )
    process.coordinator = Mock(
        update_exchange_symbol_admission=AsyncMock(return_value=frozenset())
    )
    process.gate = Mock(set_condition=Mock())

    assert await process._refresh_exchange_symbol_admission() is True
    assert await process._refresh_exchange_symbol_admission() is True

    process.execution_rest.get_exchange_info.assert_not_awaited()
    assert process.db.list_tradeable_exchange_symbols.await_count == 2
    assert process.coordinator.update_exchange_symbol_admission.await_count == 2
    process.gate.set_condition.assert_called_with("exchange_symbols", True)


@pytest.mark.asyncio
async def test_exchange_symbol_refresh_failure_closes_entries_without_raising():
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
    process._exchange_rules_synced_monotonic = asyncio.get_running_loop().time()
    process.execution_rest = Mock(get_exchange_info=AsyncMock())
    process.db = Mock(
        list_tradeable_exchange_symbols=AsyncMock(side_effect=RuntimeError("db down"))
    )
    process.coordinator = Mock(
        update_exchange_symbol_admission=AsyncMock(
            return_value=frozenset({"AKEUSDT"})
        )
    )
    process.gate = Mock(set_condition=Mock())

    assert await process._refresh_exchange_symbol_admission() is False

    process.coordinator.update_exchange_symbol_admission.assert_awaited_once_with(
        frozenset()
    )
    process.gate.set_condition.assert_called_once_with("exchange_symbols", False)


@pytest.mark.asyncio
async def test_daily_execution_rule_refresh_replaces_rules_without_metadata_sync():
    exchange_info = {
        "symbols": [
            {
                "symbol": "AKEUSDT",
                "contractType": "PERPETUAL",
                "status": "TRADING",
                "onboardDate": 1_700_000_000_000,
                "deliveryDate": 4_133_404_800_000,
                "filters": [],
            }
        ]
    }
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
    process._exchange_rules_synced_monotonic = (
        asyncio.get_running_loop().time()
        - EXCHANGE_RULE_REFRESH_INTERVAL_SECONDS
        - 1
    )
    process.execution_rest = Mock(
        get_exchange_info=AsyncMock(return_value=exchange_info)
    )
    process.db = Mock(
        list_tradeable_exchange_symbols=AsyncMock(return_value=["AKEUSDT"]),
    )
    process.coordinator = Mock(
        update_exchange_symbol_admission=AsyncMock(return_value=frozenset())
    )
    process.gate = Mock(set_condition=Mock())
    replacement_rules = Mock()
    process._build_symbol_rule_book = Mock(return_value=replacement_rules)

    assert await process._refresh_exchange_symbol_admission() is True

    process.execution_rest.get_exchange_info.assert_awaited_once()
    process.coordinator.update_exchange_symbol_admission.assert_awaited_once_with(
        frozenset({"AKEUSDT"}),
        symbol_rules=replacement_rules,
    )


def test_blocked_symbol_without_live_risk_does_not_close_bar_stream_gate():
    settings = SpikeLiveSettings(
        account_id="spike-test",
        symbols=["AKEUSDT", "BTCUSDT"],
        total_notional="10",
    )
    process = SpikeLiveProcess(
        settings,
        binance=Mock(),
        database=Mock(),
        redis_config=Mock(),
        strategy_config=Mock(account_id="spike-test"),
    )
    process.exchange_symbol_snapshot = ExchangeSymbolSnapshot(
        allowed_symbols=frozenset({"BTCUSDT"}),
        blocked_symbols=frozenset({"AKEUSDT"}),
        blocked_reasons={"AKEUSDT": "delivery_within_freeze_window"},
    )
    process.coordinator = Mock(
        account=Mock(symbols_with_live_risk=Mock(return_value=set()))
    )
    process.gate = Mock(set_condition=Mock())
    process._last_bar_received_monotonic = {"BTCUSDT": 99.0}

    assert process._market_symbols() == ("BTCUSDT",)
    assert process._refresh_bar_stream_gate(now=100.0) is True
    process.gate.set_condition.assert_called_once_with("bar_stream", True)


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
    process.exchange_symbol_snapshot = ExchangeSymbolSnapshot(
        allowed_symbols=frozenset({"AKEUSDT", "BTCUSDT"}),
        blocked_symbols=frozenset(),
        blocked_reasons={},
    )

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
        reconcile_exchange_symbol_admission=AsyncMock(),
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
    coordinator.reconcile_exchange_symbol_admission.assert_awaited_once()


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
        "trading_platform.strategies.spike.main.asyncio.sleep", AsyncMock()
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


@pytest.mark.asyncio
async def test_background_task_normal_exit_is_fatal():
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
    process.start = AsyncMock()
    process.stop = AsyncMock()
    process._try_publish_fatal_status = AsyncMock()
    process._tasks = [
        asyncio.create_task(asyncio.sleep(0), name="unexpected-worker")
    ]

    with pytest.raises(
        RuntimeError,
        match="background task exited unexpectedly: unexpected-worker",
    ):
        await process.run()

    assert process.gate.condition("execution") is False
    assert risk.halted is True
    process._try_publish_fatal_status.assert_awaited_once()
    process.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_explicit_stop_event_remains_normal_shutdown():
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
    process.start = AsyncMock()
    process.stop = AsyncMock()
    process.request_stop()

    await process.run()

    assert process._runtime_fatal_reason is None
    process.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_background_failure_wins_when_explicit_stop_is_also_ready():
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
    process.start = AsyncMock()
    process.stop = AsyncMock()
    process._try_publish_fatal_status = AsyncMock()

    async def fail_worker():
        raise RuntimeError("worker failed")

    worker = asyncio.create_task(fail_worker(), name="failing-worker")
    await asyncio.sleep(0)
    process._tasks = [worker]
    process.request_stop()

    with pytest.raises(RuntimeError, match="worker failed"):
        await process.run()

    assert process.gate.condition("execution") is False
    assert risk.halted is True
    process._try_publish_fatal_status.assert_awaited_once()


@pytest.mark.asyncio
async def test_full_process_exits_fatal_when_submit_unknown_attempts_exhausted():
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
    for name in ("execution", "market", "bar_stream", "campaign"):
        process.gate.set_condition(name, True)
    risk = RiskGuard("spike-test", RiskConfig())
    resolver = Mock(
        resolve_recovered_unknowns_once=AsyncMock(
            return_value={
                "unknown-order": Resolution(
                    False, None, reason="order_not_found"
                )
            }
        )
    )
    poller = SubmitUnknownPollingService(
        resolver,
        poll_interval_seconds=0,
        max_attempts=2,
    )
    runtime = Mock(
        unknown_poller=poller,
        stop=AsyncMock(side_effect=poller.stop),
    )
    process.runtime = runtime
    process.coordinator = Mock(risk_guard=risk, stop=AsyncMock())
    process.db = Mock(upsert_strategy_runtime_status=AsyncMock(return_value=True))

    async def start_process():
        await process._publish_runtime_status()
        poller.start()
        process._tasks.append(
            asyncio.create_task(
                process._submit_unknown_fatal_loop(),
                name="spike-submit-unknown-fatal",
            )
        )

    process.start = start_process

    with pytest.raises(RuntimeError, match="SUBMIT_UNKNOWN recovery failed"):
        await process.run()

    assert resolver.resolve_recovered_unknowns_once.await_count == 2
    assert process.gate.condition("execution") is False
    assert risk.halted is True
    assert risk.halt_reason == "SUBMIT_UNKNOWN resolution attempts exhausted"
    statuses = [
        call.args[0]
        for call in process.db.upsert_strategy_runtime_status.await_args_list
    ]
    assert [status.status for status in statuses] == ["running", "fatal", "fatal"]
    assert statuses[-1].stopped_at is not None
    runtime.stop.assert_awaited_once()


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
