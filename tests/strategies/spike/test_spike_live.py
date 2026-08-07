from decimal import Decimal
import asyncio
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError

from trading_platform.shared.binance.strategy_account import BinanceStrategyAccount
from trading_platform.shared.events import OrderIntent
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


def test_settings_default_to_testnet_and_live_requires_exact_confirmation():
    settings = SpikeLiveSettings(
        account_id="spike-test", symbols="btcusdt", total_notional="100"
    )
    assert settings.mode == "testnet"
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
    assert executor.submit.await_args_list[0].kwargs == {
        "reference_price": Decimal("100"),
    }
    assert executor.submit.await_args_list[1].kwargs == {
        "reference_price": Decimal("99"),
    }


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
        exit_intent, reference_price=Decimal("99")
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
