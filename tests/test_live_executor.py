from decimal import Decimal
from unittest.mock import AsyncMock, Mock

import pytest

from trading_platform.shared.binance import BinanceOrderExecutor
from trading_platform.shared.events import OrderIntent
from trading_platform.shared.execution_recovery import OrderWAL
from trading_platform.shared.risk import RiskConfig, RiskGuard


def _intent(client_order_id: str, symbol: str = "BTCUSDT") -> OrderIntent:
    return OrderIntent(
        symbol=symbol,
        side="SELL",
        price=Decimal("100"),
        quantity=Decimal("0.1"),
        client_order_id=client_order_id,
    )


def _unknown(wal: OrderWAL, client_order_id: str, *, symbol: str = "BTCUSDT"):
    intent = wal.record_intent(
        _intent(client_order_id, symbol),
        account_id="account-1",
        recorded_at=1000,
    )
    return wal.record_submit_unknown(intent, recorded_at=1001, error="timeout")


def _executor(wal: OrderWAL, guard: RiskGuard) -> BinanceOrderExecutor:
    return BinanceOrderExecutor(
        Mock(post_order=AsyncMock(), query_order=AsyncMock()),
        wal,
        account_id="account-1",
        now_ms=lambda: 2000,
        risk_guard=guard,
    )


@pytest.mark.parametrize(
    ("binance_status", "expected_status"),
    [
        ("NEW", "NEW"),
        ("PARTIALLY_FILLED", "PARTIALLY_FILLED"),
        ("FILLED", "FILLED"),
        ("CANCELED", "CANCELLED"),
        ("EXPIRED", "EXPIRED"),
    ],
)
def test_order_trade_update_resolves_owned_unknown_and_refreshes_risk(
    tmp_path, binance_status, expected_status
):
    wal = OrderWAL(tmp_path / "orders.jsonl")
    _unknown(wal, "cid-1")
    guard = RiskGuard("account-1", RiskConfig())
    guard.block_symbol("BTCUSDT", "SUBMIT_UNKNOWN:cid-1")
    executor = _executor(wal, guard)

    record = executor.handle_order_trade_update(
        {"c": "cid-1", "X": binance_status, "s": "BTCUSDT", "i": 42}
    )

    assert record is not None
    assert record.status == expected_status
    assert record.exchange_order_id == "42"
    assert wal.recover_latest()["cid-1"] == record
    assert "BTCUSDT" not in guard.blocked_symbols


def test_order_trade_update_keeps_symbol_blocked_for_another_unknown(tmp_path):
    wal = OrderWAL(tmp_path / "orders.jsonl")
    _unknown(wal, "cid-1")
    _unknown(wal, "cid-2")
    guard = RiskGuard("account-1", RiskConfig())
    guard.block_symbol("BTCUSDT", "SUBMIT_UNKNOWN pending")
    executor = _executor(wal, guard)

    executor.handle_order_trade_update(
        {"c": "cid-1", "X": "FILLED", "s": "BTCUSDT", "i": 42}
    )

    assert wal.recover_latest()["cid-2"].status == "SUBMIT_UNKNOWN"
    assert "BTCUSDT" in guard.blocked_symbols


def test_order_trade_update_explicitly_ignores_foreign_client_id(tmp_path):
    wal = OrderWAL(tmp_path / "orders.jsonl")
    _unknown(wal, "cid-1")
    guard = RiskGuard("account-1", RiskConfig())
    executor = _executor(wal, guard)
    before = (tmp_path / "orders.jsonl").read_text()

    result = executor.handle_order_trade_update(
        {"c": "manual-order", "X": "FILLED", "s": "BTCUSDT", "i": 99}
    )

    assert result is None
    assert (tmp_path / "orders.jsonl").read_text() == before


@pytest.mark.parametrize(
    "order_data",
    [
        {"c": "cid-1", "X": "PENDING_CANCEL", "s": "BTCUSDT", "i": 42},
        {"c": "cid-1", "X": "FILLED", "s": "ETHUSDT", "i": 42},
        {"c": "cid-1", "X": "FILLED", "s": "BTCUSDT"},
    ],
)
def test_order_trade_update_invalid_owned_report_fails_closed(tmp_path, order_data):
    wal = OrderWAL(tmp_path / "orders.jsonl")
    _unknown(wal, "cid-1")
    guard = RiskGuard("account-1", RiskConfig())
    executor = _executor(wal, guard)

    with pytest.raises(ValueError):
        executor.handle_order_trade_update(order_data)

    assert wal.recover_latest()["cid-1"].status == "SUBMIT_UNKNOWN"
    assert "BTCUSDT" in guard.blocked_symbols


def test_order_trade_update_rejects_illegal_terminal_state_regression(tmp_path):
    wal = OrderWAL(tmp_path / "orders.jsonl")
    unknown = _unknown(wal, "cid-1")
    wal.record_exchange_status(
        unknown,
        {"status": "FILLED", "orderId": 42},
        recorded_at=1002,
    )
    guard = RiskGuard("account-1", RiskConfig())
    executor = _executor(wal, guard)

    with pytest.raises(ValueError, match="invalid status transition"):
        executor.handle_order_trade_update(
            {"c": "cid-1", "X": "NEW", "s": "BTCUSDT", "i": 42}
        )

    assert wal.recover_latest()["cid-1"].status == "FILLED"
    assert "BTCUSDT" in guard.blocked_symbols
