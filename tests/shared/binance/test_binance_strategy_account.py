from decimal import Decimal
from unittest.mock import AsyncMock, Mock

import pytest

from trading_platform.shared.binance.strategy_account import BinanceStrategyAccount
from trading_platform.shared.events import OrderIntent
from trading_platform.shared.execution_recovery import OrderWAL
from trading_platform.shared.risk import RiskConfig, RiskGuard


def _account(tmp_path):
    rest = Mock(cancel_order=AsyncMock(), get_position_risk=AsyncMock(return_value=[]))
    wal = OrderWAL(tmp_path / "orders.jsonl")
    risk = RiskGuard("spike-test", RiskConfig())
    return BinanceStrategyAccount(
        rest,
        wal,
        account_id="spike-test",
        strategy_id="spike_short",
        risk_guard=risk,
        now_ms=lambda: 2_000,
    ), rest, wal, risk


def _intent(client_order_id="cid-1"):
    return OrderIntent(
        symbol="BTCUSDT",
        side="SELL",
        price=Decimal("100"),
        quantity=Decimal("1"),
        client_order_id=client_order_id,
        ttl_ms=10_000,
        strategy_id="spike_short",
        trigger_reason="spike_tier1",
    )


@pytest.mark.asyncio
async def test_sync_cancel_is_flushed_to_exchange_and_wal(tmp_path):
    account, rest, wal, _ = _account(tmp_path)
    intent_record = wal.record_intent(_intent(), account_id="spike-test", recorded_at=1_000)
    wal.record_exchange_status(
        intent_record, {"status": "NEW", "orderId": 42}, recorded_at=1_100
    )
    rest.cancel_order.return_value = {"status": "CANCELED", "orderId": 42}

    assert account.cancel_order("42") is True
    assert await account.flush_cancellations() == ("cid-1",)
    assert wal.recover_latest()["cid-1"].status == "CANCELLED"


@pytest.mark.asyncio
async def test_cancel_failure_blocks_symbol_and_keeps_request(tmp_path):
    account, rest, wal, risk = _account(tmp_path)
    intent_record = wal.record_intent(_intent(), account_id="spike-test", recorded_at=1_000)
    wal.record_exchange_status(
        intent_record, {"status": "NEW", "orderId": 42}, recorded_at=1_100
    )
    rest.cancel_order.side_effect = RuntimeError("network")

    assert account.cancel_order("42") is True
    assert await account.flush_cancellations() == ()
    assert "BTCUSDT" in risk.blocked_symbols
    assert wal.recover_latest()["cid-1"].status == "NEW"


@pytest.mark.asyncio
async def test_newer_stream_position_is_not_overwritten_by_old_rest_snapshot(tmp_path):
    account, rest, _, _ = _account(tmp_path)
    await account.handle_account_update(
        {
            "e": "ACCOUNT_UPDATE",
            "T": 2_000,
            "a": {
                "P": [
                    {"s": "BTCUSDT", "pa": "-2", "ep": "100", "up": "5", "ps": "BOTH"}
                ]
            },
        }
    )
    rest.get_position_risk.return_value = [
        {
            "symbol": "BTCUSDT",
            "positionAmt": "0",
            "entryPrice": "0",
            "unRealizedProfit": "0",
            "positionSide": "BOTH",
            "updateTime": 1_000,
        }
    ]

    await account.refresh_positions()

    position = account.get_position("BTCUSDT")
    assert position is not None
    assert position.side == "SHORT"
    assert position.quantity == Decimal("2")


def test_trade_report_returns_strategy_fill(tmp_path):
    account, _, wal, _ = _account(tmp_path)
    intent_record = wal.record_intent(_intent(), account_id="spike-test", recorded_at=1_000)
    wal.record_exchange_status(
        intent_record, {"status": "PARTIALLY_FILLED", "orderId": 42}, recorded_at=1_100
    )

    fill = account.handle_execution_report(
        {
            "c": "cid-1",
            "x": "TRADE",
            "l": "0.25",
            "L": "101",
            "n": "0.01",
            "N": "USDT",
            "t": 7,
            "T": 1_200,
            "m": True,
        }
    )

    assert fill is not None
    assert fill.order_id == "42"
    assert fill.quantity == Decimal("0.25")


def test_unresolved_intent_keeps_account_fail_closed(tmp_path):
    account, _, wal, _ = _account(tmp_path)
    wal.record_intent(_intent(), account_id="spike-test", recorded_at=1_000)
    assert account.has_unresolved_orders() is True

    record = wal.recover_latest()["cid-1"]
    wal.record_exchange_status(
        record, {"status": "NEW", "orderId": 42}, recorded_at=1_100
    )
    assert account.has_unresolved_orders() is False
