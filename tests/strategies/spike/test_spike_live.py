from decimal import Decimal
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError

from trading_platform.shared.events import OrderIntent
from trading_platform.shared.risk import RiskConfig, RiskGuard
from trading_platform.strategies.spike_live import (
    LIVE_CONFIRMATION,
    CompositeEntryGate,
    SpikeExecutionCoordinator,
    SpikeLiveSettings,
    SpikeRuntimeCallbacks,
    require_one_way_position_mode,
)
from trading_platform.strategies.spike_main import SpikeLiveProcess


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
        strategy_id="spike_short",
        trigger_reason="campaign_timeout_exit",
    )

    await coordinator._execute([_entry(), exit_intent], event_time=1_001)

    store.acquire.assert_awaited_once()
    assert executor.submit.await_args_list[0].kwargs == {
        "reduce_only": False,
        "reference_price": Decimal("100"),
    }
    assert executor.submit.await_args_list[1].kwargs == {
        "reduce_only": True,
        "reference_price": Decimal("99"),
    }


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
async def test_shutdown_cancels_every_open_entry_order():
    strategy = StrategyStub()
    gate = CompositeEntryGate(strategy)
    open_order = Mock(
        order_id="11",
        client_order_id="entry-1",
        trigger_reason="spike_tier1",
        status="NEW",
    )
    closed_order = Mock(
        order_id="11",
        client_order_id="entry-1",
        trigger_reason="spike_tier1",
        status="CANCELLED",
    )
    account = Mock(
        iter_orders=Mock(side_effect=[(open_order,), (closed_order,)]),
        cancel_order=Mock(return_value=True),
        flush_cancellations=AsyncMock(return_value=("entry-1",)),
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
        status="NEW",
        created_at=1_000,
        ttl_ms=100,
    )
    account = Mock(
        iter_orders=Mock(return_value=(expired,)),
        cancel_order=Mock(return_value=True),
        flush_cancellations=AsyncMock(return_value=("entry-expired",)),
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
