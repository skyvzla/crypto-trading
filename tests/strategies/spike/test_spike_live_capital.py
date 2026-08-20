import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError

from trading_platform.shared.events import OrderIntent
from trading_platform.shared.risk import RiskConfig, RiskGuard
from trading_platform.strategies.campaign_store import CampaignLease
from trading_platform.strategies.admission import SubcategoryAdmissionService
from trading_platform.strategies.spike.capital import (
    CapitalPolicyConfig,
    CapitalState,
)
from trading_platform.strategies.spike.capital_store import (
    CapitalConfigurationConflictError,
    CapitalSnapshot,
)
from trading_platform.strategies.spike.live import (
    CompositeEntryGate,
    SpikeExecutionCoordinator,
    SpikeLiveSettings,
    campaign_store_key,
)
from trading_platform.strategies.spike.main import (
    SpikeLiveProcess,
    require_viable_entry_notional,
)


class StrategyStub:
    def __init__(self):
        self.enabled = False
        self.strategies = {
            "BTCUSDT": SimpleNamespace(total_notional=Decimal("1")),
            "ETHUSDT": SimpleNamespace(total_notional=Decimal("1")),
        }

    def set_entry_enabled(self, enabled):
        self.enabled = enabled

    def set_execution_enabled(self, _enabled):
        return None

    def drain_audit_events(self):
        return []


def capital_config(*, minimum="10"):
    return CapitalPolicyConfig(
        initial_account_capital="100",
        initial_trading_capital="50",
        profit_reinvest_ratio="0.5",
        minimum_trading_capital=minimum,
    )


def capital_snapshot(*, trading="50", reserve="50", version=1):
    config = capital_config()
    state = CapitalState(
        account_capital=Decimal(trading) + Decimal(reserve),
        trading_capital=Decimal(trading),
        reserve_capital=Decimal(reserve),
    )
    return CapitalSnapshot(
        account_id="account-a",
        strategy_id="spike_short",
        config=config,
        state=state,
        version=version,
        updated_at=datetime(2026, 8, 20, tzinfo=UTC),
    )


def test_formal_capital_settings_override_legacy_notional():
    settings = SpikeLiveSettings(
        account_id="account-a",
        symbols="btcusdt",
        total_notional="999",
        initial_account_capital="100",
        initial_trading_capital="50",
        profit_reinvest_ratio="0.5",
        minimum_trading_capital="10",
    )

    assert settings.entry_tier_mode == "single-entry"
    assert settings.capital_config == capital_config()
    assert settings.initial_order_notional == Decimal("50")


def test_legacy_total_notional_remains_a_complete_compatibility_configuration():
    settings = SpikeLiveSettings(
        account_id="account-a", symbols=["BTCUSDT"], total_notional="20"
    )

    assert settings.capital_config == CapitalPolicyConfig(
        initial_account_capital="20",
        initial_trading_capital="20",
        profit_reinvest_ratio="1",
        minimum_trading_capital="0",
    )
    assert settings.initial_order_notional == Decimal("20")


@pytest.mark.asyncio
async def test_live_process_rejects_legacy_capital_before_database_initialization():
    settings = SpikeLiveSettings(
        account_id="account-a", symbols=["BTCUSDT"], total_notional="20"
    )
    process = SpikeLiveProcess(
        settings,
        binance=Mock(),
        database=Mock(),
        redis_config=Mock(),
        strategy_config=Mock(account_id="account-a"),
    )

    with pytest.raises(ValueError, match="all four formal capital policy fields"):
        await process._initialize_capital(Mock())


def test_partial_formal_capital_configuration_is_rejected():
    with pytest.raises(ValidationError, match="must be configured together"):
        SpikeLiveSettings(
            account_id="account-a",
            symbols=["BTCUSDT"],
            total_notional="20",
            initial_account_capital="100",
        )


def test_live_rejects_new_three_tier_entries():
    with pytest.raises(ValidationError, match="must be single-entry"):
        SpikeLiveSettings(
            account_id="account-a",
            symbols=["BTCUSDT"],
            total_notional="20",
            entry_tier_mode="three-tier",
        )


def test_single_entry_notional_gate_uses_the_full_trading_capital():
    rules = Mock(symbol="BTCUSDT", min_notional=Decimal("5"))

    require_viable_entry_notional(
        Decimal("10"), rules, entry_tier_mode="single-entry"
    )

    with pytest.raises(ValueError, match="entry notional"):
        require_viable_entry_notional(
            Decimal("5"), rules, entry_tier_mode="single-entry"
        )


def test_campaign_store_key_is_scoped_by_account_and_strategy():
    assert campaign_store_key("account-a", "spike_short") == (
        "trading_platform:campaign:account-a:spike_short:active"
    )
    assert campaign_store_key("account-b", "spike_short") != campaign_store_key(
        "account-a", "spike_short"
    )


def coordinator_for_settlement(*, capital_store, funding_source, refresh=None):
    strategy = StrategyStub()
    gate = CompositeEntryGate(strategy)
    gate.set_condition("campaign", False)
    account = Mock(
        has_open_position=Mock(return_value=False),
        has_pending_position_update=Mock(return_value=False),
        all_orders_terminal=Mock(return_value=True),
    )
    campaign_id = "spike_short:BTCUSDT:1000"
    closed_at = datetime(2026, 8, 20, 8, 30, tzinfo=UTC)
    summary = SimpleNamespace(
        symbol="BTCUSDT",
        campaign_id=campaign_id,
        net_realized_pnl=Decimal("20"),
        has_open_quantity=False,
        closed_at=closed_at,
    )
    trade_source = Mock(get_campaign_pnl=AsyncMock(return_value=summary))
    campaign_store = Mock(release=AsyncMock(return_value=True))
    coordinator = SpikeExecutionCoordinator(
        strategy=strategy,
        account=account,
        executor=Mock(),
        campaign_store=campaign_store,
        risk_guard=RiskGuard("account-a", RiskConfig()),
        gate=gate,
        account_id="account-a",
        trade_source=trade_source,
        capital_store=capital_store,
        funding_source=funding_source,
        capital_admission_refresh=refresh,
        now_ms=lambda: 2_000,
    )
    lease = CampaignLease(
        campaign_id,
        "spike_short",
        "BTCUSDT",
        1_000,
    )
    coordinator._owned_campaign_id = campaign_id
    coordinator._owned_campaign_lease = lease
    return coordinator, campaign_store, trade_source, closed_at


@pytest.mark.asyncio
async def test_campaign_settlement_includes_funding_and_updates_next_notional():
    next_snapshot = capital_snapshot(trading="59", reserve="59", version=2)
    result = SimpleNamespace(snapshot=next_snapshot)
    capital_store = Mock(settle=AsyncMock(return_value=result))
    funding_source = Mock(
        sync_funding_fee_total=AsyncMock(return_value=Decimal("-2"))
    )
    refresh = AsyncMock(return_value=True)
    coordinator, campaign_store, _, closed_at = coordinator_for_settlement(
        capital_store=capital_store,
        funding_source=funding_source,
        refresh=refresh,
    )

    assert await coordinator.maybe_release_campaign("BTCUSDT") is True

    funding_source.sync_funding_fee_total.assert_awaited_once_with(
        account_id="account-a",
        symbol="BTCUSDT",
        start_at=datetime.fromtimestamp(1, tz=UTC),
        end_at=closed_at.replace(microsecond=1_000),
    )
    capital_store.settle.assert_awaited_once_with(
        account_id="account-a",
        strategy_id="spike_short",
        idempotency_key="spike_short:BTCUSDT:1000",
        campaign_id="spike_short:BTCUSDT:1000",
        net_pnl=Decimal("18"),
        occurred_at=closed_at,
    )
    assert {
        child.total_notional for child in coordinator.strategy.strategies.values()
    } == {Decimal("59")}
    assert coordinator.risk_guard.config.max_position_value_usdt == Decimal("59")
    assert coordinator.risk_guard.check_can_open(
        "BTCUSDT", Decimal("59")
    ) == (True, "ok")
    refresh.assert_awaited_once_with(next_snapshot)
    campaign_store.release.assert_awaited_once()


@pytest.mark.asyncio
async def test_duplicate_release_retry_reuses_campaign_idempotency_key():
    next_snapshot = capital_snapshot(trading="59", reserve="59", version=2)
    capital_store = Mock(
        settle=AsyncMock(return_value=SimpleNamespace(snapshot=next_snapshot))
    )
    funding_source = Mock(
        sync_funding_fee_total=AsyncMock(return_value=Decimal("-2"))
    )
    coordinator, campaign_store, _, _ = coordinator_for_settlement(
        capital_store=capital_store, funding_source=funding_source
    )
    campaign_store.release.side_effect = [False, True]

    assert await coordinator.maybe_release_campaign("BTCUSDT") is False
    assert await coordinator.maybe_release_campaign("BTCUSDT") is True

    assert capital_store.settle.await_count == 2
    assert {
        call.kwargs["idempotency_key"]
        for call in capital_store.settle.await_args_list
    } == {"spike_short:BTCUSDT:1000"}


@pytest.mark.asyncio
async def test_minimum_capital_closes_entries_without_blocking_release():
    stopped_snapshot = CapitalSnapshot(
        account_id="account-a",
        strategy_id="spike_short",
        config=capital_config(minimum="10"),
        state=CapitalState(
            account_capital=Decimal("60"),
            trading_capital=Decimal("10"),
            reserve_capital=Decimal("50"),
        ),
        version=2,
        updated_at=datetime(2026, 8, 20, tzinfo=UTC),
    )
    capital_store = Mock(
        settle=AsyncMock(return_value=SimpleNamespace(snapshot=stopped_snapshot))
    )
    coordinator, campaign_store, _, _ = coordinator_for_settlement(
        capital_store=capital_store,
        funding_source=Mock(
            sync_funding_fee_total=AsyncMock(return_value=Decimal("0"))
        ),
    )

    assert await coordinator.maybe_release_campaign("BTCUSDT") is True

    assert coordinator.gate.condition("capital") is False
    campaign_store.release.assert_awaited_once()


@pytest.mark.asyncio
async def test_settlement_failure_never_releases_campaign():
    capital_store = Mock(settle=AsyncMock(side_effect=RuntimeError("postgres down")))
    coordinator, campaign_store, _, _ = coordinator_for_settlement(
        capital_store=capital_store,
        funding_source=Mock(
            sync_funding_fee_total=AsyncMock(return_value=Decimal("0"))
        ),
    )

    with pytest.raises(RuntimeError, match="postgres down"):
        await coordinator.maybe_release_campaign("BTCUSDT")

    campaign_store.release.assert_not_awaited()
    assert coordinator._owned_campaign_id == "spike_short:BTCUSDT:1000"
    assert coordinator.gate.condition("capital") is False


def live_process():
    settings = SpikeLiveSettings(
        account_id="account-a",
        symbols=["BTCUSDT"],
        initial_account_capital="100",
        initial_trading_capital="50",
        profit_reinvest_ratio="0.5",
        minimum_trading_capital="10",
    )
    process = SpikeLiveProcess(
        settings,
        binance=Mock(),
        database=Mock(),
        redis_config=Mock(),
        strategy_config=Mock(account_id="account-a"),
    )
    strategy = StrategyStub()
    process.gate = CompositeEntryGate(strategy)
    process.coordinator = Mock(record_capital_admission=AsyncMock())
    process.execution_rest = Mock()
    return process


@pytest.mark.asyncio
async def test_startup_initializes_persisted_capital_and_rejects_config_drift(
    monkeypatch,
):
    process = live_process()
    store = Mock(initialize=AsyncMock(return_value=capital_snapshot()))
    capital_store_type = Mock(return_value=store)
    monkeypatch.setattr(
        "trading_platform.strategies.spike.main.CapitalStore",
        capital_store_type,
    )
    pool = Mock()

    assert await process._initialize_capital(pool) == capital_snapshot()
    capital_store_type.assert_called_once_with(pool)
    store.initialize.assert_awaited_once_with(
        account_id="account-a",
        strategy_id="spike_short",
        config=capital_config(),
    )

    store.initialize.side_effect = CapitalConfigurationConflictError(
        "capital policy differs"
    )
    with pytest.raises(CapitalConfigurationConflictError):
        await process._initialize_capital(pool)


def test_process_builds_funding_sync_from_ledger_modules(monkeypatch):
    income_store = Mock()
    income_store_type = Mock(return_value=income_store)
    funding_sync = Mock()
    funding_sync_type = Mock(return_value=funding_sync)
    monkeypatch.setattr(
        "trading_platform.strategies.spike.main.IncomeStore", income_store_type
    )
    monkeypatch.setattr(
        "trading_platform.strategies.spike.main.FundingIncomeSync",
        funding_sync_type,
    )
    rest = Mock()
    pool = Mock()

    assert SpikeLiveProcess._build_funding_source(rest, pool) is funding_sync
    income_store_type.assert_called_once_with(pool)
    funding_sync_type.assert_called_once_with(rest, income_store)


@pytest.mark.asyncio
async def test_empty_account_wallet_shortfall_refuses_startup():
    process = live_process()
    process.execution_rest.get_account = AsyncMock(
        return_value={"totalWalletBalance": "90"}
    )

    with pytest.raises(RuntimeError, match="wallet capital is insufficient"):
        await process._reconcile_capital_wallet(
            capital_snapshot(), recovery_allowed=False
        )

    assert process.gate.condition("capital") is False


@pytest.mark.asyncio
async def test_open_campaign_wallet_shortfall_keeps_exit_recovery_running():
    process = live_process()
    process.gate.set_condition("execution", True)
    process.execution_rest.get_account = AsyncMock(
        return_value={"totalWalletBalance": "90"}
    )

    assert await process._reconcile_capital_wallet(
        capital_snapshot(), recovery_allowed=True
    ) is False

    assert process.gate.condition("capital") is False
    assert process.gate.condition("execution") is True
    process.coordinator.record_capital_admission.assert_awaited_once()


@pytest.mark.asyncio
async def test_reconnect_refreshes_positions_and_wallet_before_execution_gate():
    process = live_process()
    snapshot = capital_snapshot()
    process.capital_snapshot = snapshot
    process.capital_store = Mock(get_state=AsyncMock(return_value=snapshot))
    process.execution_rest.get_account = AsyncMock(
        return_value={"totalWalletBalance": "100"}
    )
    process.coordinator.account = Mock(
        refresh_positions=AsyncMock(),
        symbols_with_live_risk=Mock(return_value=set()),
        has_unresolved_orders=Mock(return_value=False),
    )
    process.coordinator._owned_campaign_id = None
    process.coordinator.risk_guard = RiskGuard("account-a", RiskConfig())
    process.runtime = Mock(user_stream=Mock(connected=True))
    process.execution_lease = Mock(held=True)

    assert await process._recover_execution_after_reconnect() is True

    process.coordinator.account.refresh_positions.assert_awaited_once()
    process.capital_store.get_state.assert_awaited_once_with(
        account_id="account-a", strategy_id="spike_short"
    )
    process.execution_rest.get_account.assert_awaited_once()
    assert process.gate.condition("capital") is True
    assert process.gate.condition("execution") is True


@pytest.mark.asyncio
async def test_reconnect_wallet_shortfall_keeps_existing_position_exit_enabled():
    process = live_process()
    snapshot = capital_snapshot()
    process.capital_snapshot = snapshot
    process.capital_store = Mock(get_state=AsyncMock(return_value=snapshot))
    process.execution_rest.get_account = AsyncMock(
        return_value={"totalWalletBalance": "90"}
    )
    process.coordinator.account = Mock(
        refresh_positions=AsyncMock(),
        symbols_with_live_risk=Mock(return_value={"BTCUSDT"}),
        has_unresolved_orders=Mock(return_value=False),
    )
    process.coordinator._owned_campaign_id = "spike_short:BTCUSDT:1000"
    process.coordinator.risk_guard = RiskGuard("account-a", RiskConfig())
    process.runtime = Mock(user_stream=Mock(connected=True))
    process.execution_lease = Mock(held=True)

    assert await process._recover_execution_after_reconnect() is True

    assert process.gate.condition("capital") is False
    assert process.gate.condition("execution") is True


def test_spike_entry_reason_is_part_of_live_admission():
    from trading_platform.strategies.spike.live import ENTRY_REASONS

    assert "spike_entry" in ENTRY_REASONS


@pytest.mark.asyncio
async def test_closed_subcategory_cancels_single_entry_but_preserves_exit():
    from trading_platform.strategies.spike.live import ENTRY_REASONS

    single_entry = Mock(
        order_id="entry-1",
        strategy_id="spike_short",
        trigger_reason="spike_entry",
        status="NEW",
    )
    reduce_only_exit = Mock(
        order_id="exit-1",
        strategy_id="spike_short",
        trigger_reason="candidate_time_risk_exit",
        status="NEW",
    )
    account = Mock(
        iter_orders=Mock(return_value=(single_entry, reduce_only_exit)),
        cancel_order=Mock(return_value=True),
    )
    gate = Mock(set_entry_enabled=Mock())
    admission = SubcategoryAdmissionService(
        source=Mock(is_subcategory_enabled=AsyncMock(return_value=False)),
        gate=gate,
        account=account,
        subcategory="spike",
        strategy_id="spike_short",
        entry_trigger_reasons=ENTRY_REASONS,
    )

    result = await admission.on_universe_scan()

    gate.set_entry_enabled.assert_called_once_with(False)
    account.cancel_order.assert_called_once_with("entry-1")
    assert result.cancelled_order_ids == ("entry-1",)


def _entry(symbol, signal_time):
    return OrderIntent(
        symbol=symbol,
        side="SELL",
        price=Decimal("100"),
        quantity=Decimal("1"),
        client_order_id=f"spike_short_{symbol}_{signal_time}_tier3",
        reduce_only=False,
        strategy_id="spike_short",
        trigger_reason="spike_entry",
    )


@pytest.mark.asyncio
async def test_inflight_submit_blocks_release_but_not_fill_state_update():
    strategy = StrategyStub()
    strategy.on_fill = Mock()
    gate = CompositeEntryGate(strategy)
    for condition in ("execution", "campaign"):
        gate.set_condition(condition, True)
    release_submit = asyncio.Event()
    submit_started = asyncio.Event()

    async def slow_submit(*_args, **_kwargs):
        submit_started.set()
        await release_submit.wait()
        return Mock(status="NEW")

    account = Mock(
        has_open_position=Mock(return_value=False),
        has_pending_position_update=Mock(return_value=False),
        all_orders_terminal=Mock(return_value=True),
    )
    coordinator = SpikeExecutionCoordinator(
        strategy=strategy,
        account=account,
        executor=Mock(submit=AsyncMock(side_effect=slow_submit)),
        campaign_store=Mock(
            get_active=AsyncMock(return_value=None),
            acquire=AsyncMock(return_value=True),
            release=AsyncMock(return_value=True),
        ),
        risk_guard=RiskGuard("account-a", RiskConfig()),
        gate=gate,
        account_id="account-a",
    )
    submit_task = asyncio.create_task(
        coordinator._execute([_entry("BTCUSDT", 1_000)], event_time=1_001)
    )
    await asyncio.wait_for(submit_started.wait(), timeout=1)

    fill = Mock(symbol="BTCUSDT")
    await asyncio.wait_for(coordinator.on_fill(fill), timeout=0.1)
    assert await coordinator.maybe_release_campaign("BTCUSDT") is False
    strategy.on_fill.assert_called_once_with(fill)

    release_submit.set()
    await submit_task
