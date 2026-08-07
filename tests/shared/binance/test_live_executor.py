from decimal import Decimal
from unittest.mock import AsyncMock, Mock

import pytest

from trading_platform.shared.binance import (
    BinanceOrderExecutor,
    BinanceSymbolRuleBook,
    BinanceSymbolRules,
)
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


def _rules() -> BinanceSymbolRuleBook:
    return BinanceSymbolRuleBook(
        {
            "BTCUSDT": BinanceSymbolRules(
                symbol="BTCUSDT",
                tick_size=Decimal("0.1"),
                min_price=Decimal("0.1"),
                max_price=Decimal("1000000"),
                lot_step_size=Decimal("0.01"),
                min_quantity=Decimal("0.01"),
                max_quantity=Decimal("100"),
                market_step_size=Decimal("0.01"),
                market_min_quantity=Decimal("0.01"),
                market_max_quantity=Decimal("100"),
                min_notional=Decimal("5"),
            )
        }
    )


@pytest.mark.asyncio
async def test_submit_normalizes_before_wal_and_exchange_request(tmp_path):
    wal = OrderWAL(tmp_path / "orders.jsonl")
    rest = Mock(
        post_order=AsyncMock(return_value={"status": "NEW", "orderId": 42}),
        query_order=AsyncMock(),
    )
    executor = BinanceOrderExecutor(
        rest,
        wal,
        account_id="account-1",
        risk_guard=RiskGuard("account-1", RiskConfig()),
        symbol_rules=_rules(),
    )
    candidate = _intent("cid-normalized")
    candidate.price = Decimal("100.01")
    candidate.quantity = Decimal("0.109")

    record = await executor.submit(candidate)

    assert record.price == "100.1"
    assert record.quantity == "0.10"
    rest.post_order.assert_awaited_once_with(
        symbol="BTCUSDT",
        side="SELL",
        order_type="LIMIT",
        quantity=Decimal("0.10"),
        price=Decimal("100.1"),
        new_client_order_id="cid-normalized",
        reduce_only=False,
    )


@pytest.mark.asyncio
async def test_risk_rejection_happens_before_wal_or_rest_submission(tmp_path):
    wal = OrderWAL(tmp_path / "orders.jsonl")
    rest = Mock(post_order=AsyncMock(), query_order=AsyncMock())
    executor = BinanceOrderExecutor(
        rest,
        wal,
        account_id="account-1",
        risk_guard=RiskGuard(
            "account-1",
            RiskConfig(max_position_value_usdt=Decimal("5")),
        ),
        symbol_rules=_rules(),
    )

    with pytest.raises(PermissionError, match="risk guard"):
        await executor.submit(_intent("cid-rejected"))

    assert wal.recover_latest() == {}
    rest.post_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_reduce_only_intent_is_the_live_exchange_contract(tmp_path):
    wal = OrderWAL(tmp_path / "orders.jsonl")
    rest = Mock(
        post_order=AsyncMock(return_value={"status": "NEW", "orderId": 42}),
        query_order=AsyncMock(),
    )
    guard = RiskGuard(
        "account-1", RiskConfig(max_position_value_usdt=Decimal("1"))
    )
    intent = _intent("cid-reduce")
    intent.side = "BUY"
    intent.reduce_only = True
    executor = BinanceOrderExecutor(
        rest, wal, account_id="account-1", risk_guard=guard
    )

    record = await executor.submit(intent)

    assert record.payload["reduce_only"] is True
    rest.post_order.assert_awaited_once_with(
        symbol="BTCUSDT",
        side="BUY",
        order_type="LIMIT",
        quantity=Decimal("0.1"),
        price=Decimal("100"),
        new_client_order_id="cid-reduce",
        reduce_only=True,
    )


@pytest.mark.asyncio
async def test_reused_client_id_with_different_intent_fails_closed(tmp_path):
    wal = OrderWAL(tmp_path / "orders.jsonl")
    rest = Mock(
        post_order=AsyncMock(return_value={"status": "NEW", "orderId": 42}),
        query_order=AsyncMock(),
    )
    guard = RiskGuard("account-1", RiskConfig())
    executor = BinanceOrderExecutor(
        rest,
        wal,
        account_id="account-1",
        risk_guard=guard,
    )
    await executor.submit(_intent("cid-reused"))
    changed = _intent("cid-reused")
    changed.quantity = Decimal("0.2")

    with pytest.raises(ValueError, match="different intent"):
        await executor.submit(changed)

    assert rest.post_order.await_count == 1
    assert "BTCUSDT" in guard.blocked_symbols


def test_startup_query_response_advances_owned_wal_and_unblocks_symbol(tmp_path):
    wal = OrderWAL(tmp_path / "orders.jsonl")
    unknown = _unknown(wal, "cid-recovered")
    guard = RiskGuard("account-1", RiskConfig())
    guard.block_symbol("BTCUSDT", "SUBMIT_UNKNOWN")
    executor = _executor(wal, guard)

    record = executor.reconcile_order_response(
        {
            "clientOrderId": unknown.client_order_id,
            "symbol": "BTCUSDT",
            "side": "SELL",
            "type": "LIMIT",
            "origQty": "0.1",
            "price": "100",
            "status": "FILLED",
            "orderId": 77,
            "executedQty": "0.1",
        }
    )

    assert record.status == "FILLED"
    assert record.exchange_order_id == "77"
    assert "BTCUSDT" not in guard.blocked_symbols


def test_startup_query_identity_mismatch_stays_fail_closed(tmp_path):
    wal = OrderWAL(tmp_path / "orders.jsonl")
    _unknown(wal, "cid-recovered")
    guard = RiskGuard("account-1", RiskConfig())
    executor = _executor(wal, guard)

    with pytest.raises(ValueError, match="quantity mismatch"):
        executor.reconcile_order_response(
            {
                "clientOrderId": "cid-recovered",
                "symbol": "BTCUSDT",
                "side": "SELL",
                "type": "LIMIT",
                "origQty": "0.2",
                "status": "NEW",
                "orderId": 77,
            }
        )

    assert wal.recover_latest()["cid-recovered"].status == "SUBMIT_UNKNOWN"
    assert "BTCUSDT" in guard.blocked_symbols


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
