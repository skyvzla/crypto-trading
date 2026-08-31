from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, Mock

import pytest

from trading_platform.ledger.db.models import Trade
from trading_platform.shared.events import Bar1s, Position
from trading_platform.shared.execution_recovery import OrderWAL, OrderWALRecord
from trading_platform.shared.risk import RiskConfig, RiskGuard
from trading_platform.strategies.spike.live import (
    CompositeEntryGate,
    SpikeExecutionCoordinator,
)
from trading_platform.strategies.spike.exit_policy import CANDIDATE_FULL_EXIT_REASONS
from trading_platform.strategies.campaign_store import CampaignLease
from trading_platform.strategies.spike.short import DynamicSpikeBacktestStrategy


CAMPAIGN_ID = "spike_short:AKEUSDT:1000"


def _wal_record(
    client_order_id: str,
    *,
    recorded_at: int,
    reason: str,
    campaign_id: str = CAMPAIGN_ID,
) -> OrderWALRecord:
    return OrderWALRecord(
        record_type="exchange_status",
        recorded_at=recorded_at,
        account_id="spike-test",
        client_order_id=client_order_id,
        symbol="AKEUSDT",
        side="SELL" if reason.startswith("spike_tier") else "BUY",
        order_type="LIMIT" if reason.startswith("spike_tier") else "MARKET",
        quantity="1",
        price="1",
        status="FILLED",
        exchange_order_id=client_order_id,
        payload={
            "strategy_id": "spike_short",
            "trigger_reason": reason,
            "campaign_id": campaign_id,
        },
    )


def _coordinator(tmp_path, *, trades: list[Trade], exit_policy="execution-test-d007"):
    wal = OrderWAL(tmp_path / "orders.jsonl")
    entry_id = "s_AKEUSDT_rs_e1"
    wal.append(_wal_record(entry_id, recorded_at=1_100, reason="spike_tier1"))
    wal.append(
        _wal_record(
            "x_AKEUSDT_12kw_r",
            recorded_at=1_500,
            reason="campaign_rotation_exit",
        )
    )
    position = Position(
        symbol="AKEUSDT",
        side="SHORT",
        entry_price=Decimal("1"),
        quantity=Decimal("10"),
        total_commission=Decimal("0"),
        unrealized_pnl=Decimal("0"),
        realized_pnl=Decimal("0"),
        opened_at=2_000,
    )
    account = Mock(
        wal=wal,
        symbols_with_live_risk=Mock(return_value={"AKEUSDT"}),
        get_position=Mock(return_value=position),
        restore_trade_state=Mock(),
    )
    strategy = DynamicSpikeBacktestStrategy(
        ["AKEUSDT"],
        Decimal("20"),
        account=account,
        exit_policy=exit_policy,
    )
    gate = CompositeEntryGate(strategy)
    coordinator = SpikeExecutionCoordinator(
        strategy=strategy,
        account=account,
        executor=Mock(),
        campaign_store=Mock(),
        risk_guard=RiskGuard("spike-test", RiskConfig()),
        gate=gate,
        account_id="spike-test",
        trade_source=Mock(
            get_trades_by_client_order_ids=AsyncMock(return_value=trades)
        ),
    )
    coordinator._owned_campaign_id = CAMPAIGN_ID
    return coordinator, strategy, account, entry_id


def _entry_trade(entry_id: str) -> Trade:
    return Trade(
        account_id="spike-test",
        strategy_id="spike_short",
        symbol="AKEUSDT",
        trade_id="1",
        client_order_id=entry_id,
        campaign_id=CAMPAIGN_ID,
        side="SELL",
        commission=Decimal("0.01"),
        exchange_time=datetime.fromtimestamp(1.2, timezone.utc),
    )


def _exit_trade(client_order_id: str) -> Trade:
    return Trade(
        account_id="spike-test",
        strategy_id="spike_short",
        symbol="AKEUSDT",
        trade_id="2",
        client_order_id=client_order_id,
        campaign_id=CAMPAIGN_ID,
        side="BUY",
        quantity=Decimal("1"),
        commission=Decimal("0.01"),
        exchange_time=datetime.fromtimestamp(1.3, timezone.utc),
    )


@pytest.mark.asyncio
async def test_restart_restores_first_fill_and_all_owned_campaign_commission(tmp_path):
    entry_time = datetime.fromtimestamp(1.2, timezone.utc)
    exit_time = datetime.fromtimestamp(1.6, timezone.utc)
    coordinator, strategy, account, entry_id = _coordinator(
        tmp_path,
        trades=[
            Trade(
                account_id="spike-test",
                strategy_id="spike_short",
                symbol="AKEUSDT",
                trade_id="1",
                client_order_id="s_AKEUSDT_rs_e1",
                campaign_id=CAMPAIGN_ID,
                side="SELL",
                commission=Decimal("0.01"),
                exchange_time=entry_time,
            ),
            Trade(
                account_id="spike-test",
                strategy_id="spike_short",
                symbol="AKEUSDT",
                trade_id="2",
                client_order_id="x_AKEUSDT_12kw_r",
                campaign_id=CAMPAIGN_ID,
                side="BUY",
                commission=Decimal("0.02"),
                exchange_time=exit_time,
            ),
        ],
    )

    await coordinator.restore_campaign_timing()

    symbol_strategy = strategy.strategies["AKEUSDT"]
    assert symbol_strategy.first_fill_time == 1_200
    assert symbol_strategy._campaign_id_for_timing == "spike_short:AKEUSDT:1000"
    assert strategy.active_symbol == "AKEUSDT"
    account.restore_trade_state.assert_called_once_with(
        "AKEUSDT", Decimal("0.03"), {"1", "2"}
    )
    requested_ids = coordinator.trade_source.get_trades_by_client_order_ids.await_args.kwargs[
        "client_order_ids"
    ]
    assert set(requested_ids) == {"s_AKEUSDT_rs_e1", "x_AKEUSDT_12kw_r"}
    assert (
        coordinator.trade_source.get_trades_by_client_order_ids.await_args.kwargs[
            "campaign_id"
        ]
        == CAMPAIGN_ID
    )


@pytest.mark.asyncio
async def test_candidate_restart_derives_origin_reduction_from_wal(tmp_path):
    coordinator, strategy, account, entry_id = _coordinator(
        tmp_path, trades=[], exit_policy="candidate-v1"
    )
    account.wal.append(
        _wal_record(
            "x_AKEUSDT_reduce_h",
            recorded_at=1_300,
            reason="candidate_origin_reduce",
        )
    )
    coordinator.trade_source.get_trades_by_client_order_ids.return_value = [
        _entry_trade(entry_id),
        _exit_trade("x_AKEUSDT_reduce_h"),
    ]
    coordinator._owned_campaign_lease = CampaignLease(
        "spike_short:AKEUSDT:1000",
        "spike_short",
        "AKEUSDT",
        1_000,
        origin_price="0.9",
        origin_checked=True,
        reduced_at_origin=True,
    )

    await coordinator.restore_campaign_timing()

    assert strategy.strategies["AKEUSDT"].campaign_exit_state() == (
        True,
        True,
        False,
    )


@pytest.mark.asyncio
async def test_candidate_restart_rejects_redis_reduction_without_wal_order(tmp_path):
    coordinator, _, _, entry_id = _coordinator(
        tmp_path, trades=[], exit_policy="candidate-v1"
    )
    coordinator.trade_source.get_trades_by_client_order_ids.return_value = [
        _entry_trade(entry_id)
    ]
    coordinator._owned_campaign_lease = CampaignLease(
        "spike_short:AKEUSDT:1000",
        "spike_short",
        "AKEUSDT",
        1_000,
        origin_price="0.9",
        origin_checked=True,
        reduced_at_origin=True,
    )

    with pytest.raises(RuntimeError, match="no matching WAL order"):
        await coordinator.restore_campaign_timing()

    assert coordinator.gate.enabled is False


@pytest.mark.asyncio
async def test_candidate_restart_rejects_wal_reduction_without_actual_trade(tmp_path):
    coordinator, _, account, entry_id = _coordinator(
        tmp_path, trades=[], exit_policy="candidate-v1"
    )
    account.wal.append(
        _wal_record(
            "x_AKEUSDT_reduce_h",
            recorded_at=1_300,
            reason="candidate_origin_reduce",
        )
    )
    coordinator.trade_source.get_trades_by_client_order_ids.return_value = [
        _entry_trade(entry_id)
    ]
    coordinator._owned_campaign_lease = CampaignLease(
        "spike_short:AKEUSDT:1000",
        "spike_short",
        "AKEUSDT",
        1_000,
        origin_price="0.9",
        origin_checked=True,
        reduced_at_origin=True,
    )

    with pytest.raises(RuntimeError, match="actual trade"):
        await coordinator.restore_campaign_timing()


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_reason", sorted(CANDIDATE_FULL_EXIT_REASONS))
async def test_candidate_restart_retries_terminal_full_exit_when_position_remains(
    tmp_path, exit_reason
):
    coordinator, strategy, account, entry_id = _coordinator(
        tmp_path, trades=[], exit_policy="candidate-v1"
    )
    exit_id = "x_AKEUSDT_exit_c"
    account.wal.append(
        _wal_record(
            exit_id,
            recorded_at=1_300,
            reason=exit_reason,
        )
    )
    coordinator.trade_source.get_trades_by_client_order_ids.return_value = [
        _entry_trade(entry_id),
        _exit_trade(exit_id),
    ]
    coordinator._owned_campaign_lease = CampaignLease(
        "spike_short:AKEUSDT:1000",
        "spike_short",
        "AKEUSDT",
        1_000,
        origin_price="0.9",
        exit_requested=True,
    )

    await coordinator.restore_campaign_timing()

    assert strategy.strategies["AKEUSDT"].campaign_exit_state() == (
        False,
        False,
        False,
    )


@pytest.mark.asyncio
async def test_restart_with_position_but_no_entry_trade_fails_closed(tmp_path):
    coordinator, _, account, _ = _coordinator(tmp_path, trades=[])
    coordinator.gate.set_condition("campaign", True)

    with pytest.raises(RuntimeError, match="entry trade"):
        await coordinator.restore_campaign_timing()

    assert coordinator.gate.enabled is False
    account.restore_trade_state.assert_not_called()


@pytest.mark.asyncio
async def test_restart_with_position_but_campaign_not_in_wal_fails_closed(tmp_path):
    coordinator, _, account, _ = _coordinator(tmp_path, trades=[])
    coordinator._owned_campaign_id = "spike_short:AKEUSDT:999"
    coordinator.gate.set_condition("campaign", True)

    with pytest.raises(RuntimeError, match="WAL entry orders"):
        await coordinator.restore_campaign_timing()

    assert coordinator.gate.enabled is False
    account.restore_trade_state.assert_not_called()


@pytest.mark.asyncio
async def test_restart_rejects_trade_from_another_campaign(tmp_path):
    coordinator, _, account, entry_id = _coordinator(tmp_path, trades=[])
    trade = _entry_trade(entry_id)
    trade.campaign_id = "spike_short:AKEUSDT:999"
    coordinator.trade_source.get_trades_by_client_order_ids.return_value = [trade]

    with pytest.raises(RuntimeError, match="outside the owned Campaign"):
        await coordinator.restore_campaign_timing()

    assert coordinator.gate.enabled is False
    account.restore_trade_state.assert_not_called()


@pytest.mark.asyncio
async def test_restart_restored_timing_requests_timeout_exit_only_once(tmp_path):
    coordinator, strategy, account, _ = _coordinator(
        tmp_path,
        trades=[
            Trade(
                account_id="spike-test",
                strategy_id="spike_short",
                symbol="AKEUSDT",
                trade_id="1",
                client_order_id="s_AKEUSDT_rs_e1",
                campaign_id=CAMPAIGN_ID,
                side="SELL",
                commission=Decimal("0.01"),
                exchange_time=datetime.fromtimestamp(1.2, timezone.utc),
            )
        ],
    )
    await coordinator.restore_campaign_timing()
    symbol_strategy = strategy.strategies["AKEUSDT"]
    bar = Bar1s(
        symbol="AKEUSDT",
        timestamp=901_200,
        available_time=901_200,
        open=Decimal("2"),
        high=Decimal("2"),
        low=Decimal("2"),
        close=Decimal("2"),
        volume=Decimal("1"),
        trade_count=1,
        vwap=Decimal("2"),
    )

    first = symbol_strategy._manage_non_positive_timeout(bar)
    second = symbol_strategy._manage_non_positive_timeout(bar)

    assert len(first) == 1
    assert first[0].trigger_reason == "campaign_timeout_exit"
    assert second == []
