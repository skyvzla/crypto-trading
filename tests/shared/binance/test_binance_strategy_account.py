import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, Mock

import pytest

from trading_platform.shared.binance.strategy_account import BinanceStrategyAccount
from trading_platform.shared.binance.live_executor import BinanceOrderExecutor
from trading_platform.shared.events import OrderIntent
from trading_platform.shared.execution_recovery import OrderWAL
from trading_platform.shared.risk import RiskConfig, RiskGuard


def _account(tmp_path):
    rest = Mock(
        cancel_order=AsyncMock(),
        query_order=AsyncMock(return_value=None),
        get_position_risk=AsyncMock(return_value=[]),
    )
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
async def test_partial_fill_then_cancel_preserves_fill_and_terminal_order(tmp_path):
    account, rest, wal, risk = _account(tmp_path)
    intent_record = wal.record_intent(_intent(), account_id="spike-test", recorded_at=1_000)
    wal.record_exchange_status(
        intent_record, {"status": "NEW", "orderId": 42}, recorded_at=1_100
    )
    executor = BinanceOrderExecutor(
        rest,
        wal,
        account_id="spike-test",
        now_ms=iter([1_200, 1_300]).__next__,
        risk_guard=risk,
    )
    rest.cancel_order.return_value = {"status": "CANCELED", "orderId": 42}

    assert account.cancel_order("42") is True
    executor.handle_order_trade_update(
        {
            "c": "cid-1",
            "X": "PARTIALLY_FILLED",
            "s": "BTCUSDT",
            "i": 42,
        }
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
        }
    )

    assert await account.flush_cancellations() == ("cid-1",)
    assert fill is not None and fill.quantity == Decimal("0.25")
    assert wal.recover_latest()["cid-1"].status == "CANCELLED"


@pytest.mark.asyncio
async def test_late_cancel_response_cannot_overwrite_fill_seen_while_request_in_flight(
    tmp_path,
):
    account, rest, wal, risk = _account(tmp_path)
    intent_record = wal.record_intent(_intent(), account_id="spike-test", recorded_at=1_000)
    wal.record_exchange_status(
        intent_record, {"status": "NEW", "orderId": 42}, recorded_at=1_100
    )
    executor = BinanceOrderExecutor(
        rest,
        wal,
        account_id="spike-test",
        now_ms=lambda: 1_200,
        risk_guard=risk,
    )
    request_started = asyncio.Event()
    respond = asyncio.Event()

    async def delayed_cancel(*args, **kwargs):
        request_started.set()
        await respond.wait()
        return {"status": "CANCELED", "orderId": 42}

    rest.cancel_order.side_effect = delayed_cancel
    assert account.cancel_order("42") is True
    flush = asyncio.create_task(account.flush_cancellations())
    await request_started.wait()

    executor.handle_order_trade_update(
        {"c": "cid-1", "X": "FILLED", "s": "BTCUSDT", "i": 42}
    )
    respond.set()

    assert await flush == ("cid-1",)
    assert wal.recover_latest()["cid-1"].status == "FILLED"
    assert "BTCUSDT" not in risk.blocked_symbols


@pytest.mark.asyncio
async def test_cancel_error_after_late_fill_is_resolved_by_terminal_wal_fact(tmp_path):
    account, rest, wal, risk = _account(tmp_path)
    intent_record = wal.record_intent(_intent(), account_id="spike-test", recorded_at=1_000)
    wal.record_exchange_status(
        intent_record, {"status": "NEW", "orderId": 42}, recorded_at=1_100
    )
    executor = BinanceOrderExecutor(
        rest,
        wal,
        account_id="spike-test",
        now_ms=lambda: 1_200,
        risk_guard=risk,
    )
    request_started = asyncio.Event()
    fail_request = asyncio.Event()

    async def delayed_failure(*args, **kwargs):
        request_started.set()
        await fail_request.wait()
        raise RuntimeError("unknown order")

    rest.cancel_order.side_effect = delayed_failure
    assert account.cancel_order("42") is True
    flush = asyncio.create_task(account.flush_cancellations())
    await request_started.wait()
    executor.handle_order_trade_update(
        {"c": "cid-1", "X": "FILLED", "s": "BTCUSDT", "i": 42}
    )
    fail_request.set()

    assert await flush == ("cid-1",)
    assert wal.recover_latest()["cid-1"].status == "FILLED"
    assert "BTCUSDT" not in risk.blocked_symbols


@pytest.mark.asyncio
async def test_cancel_error_queries_exchange_and_resolves_terminal_fill(tmp_path):
    account, rest, wal, risk = _account(tmp_path)
    intent_record = wal.record_intent(_intent(), account_id="spike-test", recorded_at=1_000)
    wal.record_exchange_status(
        intent_record,
        {"status": "PARTIALLY_FILLED", "orderId": 42, "executedQty": "0.25"},
        recorded_at=1_100,
    )
    rest.cancel_order.side_effect = RuntimeError("unknown order")
    rest.query_order.return_value = {
        "status": "FILLED",
        "orderId": 42,
        "executedQty": "1",
    }

    assert account.cancel_order("42") is True
    assert await account.flush_cancellations() == ("cid-1",)
    rest.query_order.assert_awaited_once_with(
        "BTCUSDT", orig_client_order_id="cid-1"
    )
    assert wal.recover_latest()["cid-1"].status == "FILLED"
    assert "BTCUSDT" not in risk.blocked_symbols


@pytest.mark.asyncio
async def test_cancel_unknown_remains_nonterminal_and_fail_closed(tmp_path):
    account, rest, wal, risk = _account(tmp_path)
    intent_record = wal.record_intent(_intent(), account_id="spike-test", recorded_at=1_000)
    wal.record_exchange_status(
        intent_record,
        {"status": "PARTIALLY_FILLED", "orderId": 42},
        recorded_at=1_100,
    )
    rest.cancel_order.side_effect = RuntimeError("timeout")

    assert account.cancel_order("42") is True
    assert await account.flush_cancellations() == ()
    assert wal.recover_latest()["cid-1"].status == "PARTIALLY_FILLED"
    assert account.all_orders_terminal("BTCUSDT") is False
    assert "BTCUSDT" in risk.blocked_symbols


def test_trade_report_arriving_after_cancel_terminal_is_still_delivered(tmp_path):
    account, rest, wal, risk = _account(tmp_path)
    intent_record = wal.record_intent(_intent(), account_id="spike-test", recorded_at=1_000)
    cancelled = wal.record_exchange_status(
        intent_record,
        {"status": "CANCELED", "orderId": 42},
        recorded_at=1_100,
    )
    executor = BinanceOrderExecutor(
        rest,
        wal,
        account_id="spike-test",
        now_ms=lambda: 1_200,
        risk_guard=risk,
    )
    order_data = {
        "c": "cid-1",
        "X": "CANCELED",
        "s": "BTCUSDT",
        "i": 42,
        "x": "TRADE",
        "l": "0.25",
        "L": "101",
        "n": "0.01",
        "N": "USDT",
        "t": 8,
        "T": 1_150,
    }

    latest = executor.handle_order_trade_update(order_data)
    fill = account.handle_execution_report(order_data)

    assert latest is not None and latest.status == "CANCELLED"
    assert latest.recorded_at == 1_200
    assert fill is not None and fill.quantity == Decimal("0.25")
    assert wal.recover_latest()["cid-1"].status == cancelled.status
    assert account.all_orders_terminal("BTCUSDT") is False


@pytest.mark.asyncio
async def test_terminal_fill_waits_for_position_fact_before_campaign_can_release(tmp_path):
    account, _, wal, _ = _account(tmp_path)
    intent_record = wal.record_intent(_intent(), account_id="spike-test", recorded_at=1_000)
    wal.record_exchange_status(
        intent_record, {"status": "FILLED", "orderId": 42}, recorded_at=1_100
    )

    fill = account.handle_execution_report(
        {
            "c": "cid-1",
            "x": "TRADE",
            "l": "1",
            "L": "101",
            "t": 9,
            "T": 1_200,
        }
    )

    assert fill is not None
    assert account.has_open_position("BTCUSDT") is False
    assert account.has_pending_position_update("BTCUSDT") is True
    assert account.all_orders_terminal("BTCUSDT") is False

    await account.handle_account_update(
        {
            "e": "ACCOUNT_UPDATE",
            "T": 1_200,
            "a": {
                "P": [
                    {
                        "s": "BTCUSDT",
                        "pa": "-1",
                        "ep": "101",
                        "up": "0",
                        "ps": "BOTH",
                    }
                ]
            },
        }
    )

    assert account.has_open_position("BTCUSDT") is True
    assert account.has_pending_position_update("BTCUSDT") is False
    assert account.all_orders_terminal("BTCUSDT") is True


@pytest.mark.asyncio
async def test_old_position_update_cannot_confirm_fill(tmp_path):
    account, _, wal, _ = _account(tmp_path)
    intent_record = wal.record_intent(_intent(), account_id="spike-test", recorded_at=1_000)
    wal.record_exchange_status(intent_record, {"status": "FILLED", "orderId": 42}, recorded_at=1_100)
    account.handle_execution_report({"c": "cid-1", "x": "TRADE", "l": "1", "L": "101", "t": 1, "T": 2_000})

    await account.handle_account_update({
        "e": "ACCOUNT_UPDATE", "T": 1_999,
        "a": {"P": [{"s": "BTCUSDT", "pa": "-1", "ep": "101", "up": "0", "ps": "BOTH"}]},
    })

    assert account.has_pending_position_update("BTCUSDT") is True
    assert account.has_open_position("BTCUSDT") is True


@pytest.mark.asyncio
async def test_rest_position_snapshot_cannot_confirm_stream_fill(tmp_path):
    account, rest, wal, _ = _account(tmp_path)
    intent_record = wal.record_intent(_intent(), account_id="spike-test", recorded_at=1_000)
    wal.record_exchange_status(
        intent_record, {"status": "FILLED", "orderId": 42}, recorded_at=1_100
    )
    account.handle_execution_report(
        {"c": "cid-1", "x": "TRADE", "l": "1", "L": "101", "t": 1, "T": 2_000}
    )
    rest.get_position_risk.return_value = [
        {
            "symbol": "BTCUSDT",
            "positionAmt": "-1",
            "entryPrice": "101",
            "unRealizedProfit": "0",
            "positionSide": "BOTH",
            "updateTime": 2_001,
        }
    ]

    await account.refresh_positions()

    assert account.has_open_position("BTCUSDT") is True
    assert account.has_pending_position_update("BTCUSDT") is True
    assert account.all_orders_terminal("BTCUSDT") is False


@pytest.mark.asyncio
async def test_position_confirmation_is_symbol_scoped_and_non_trade_does_not_pending(tmp_path):
    account, _, wal, _ = _account(tmp_path)
    first = wal.record_intent(_intent(), account_id="spike-test", recorded_at=1_000)
    second_intent = _intent("cid-2")
    second_intent = OrderIntent(**{**second_intent.__dict__, "symbol": "ETHUSDT"})
    second = wal.record_intent(second_intent, account_id="spike-test", recorded_at=1_000)
    wal.record_exchange_status(first, {"status": "FILLED", "orderId": 42}, recorded_at=1_100)
    wal.record_exchange_status(second, {"status": "FILLED", "orderId": 43}, recorded_at=1_100)
    account.handle_execution_report({"c": "cid-1", "x": "NEW", "l": "1", "T": 2_000})
    assert account.has_pending_position_update("BTCUSDT") is False
    account.handle_execution_report({"c": "cid-1", "x": "TRADE", "l": "1", "L": "101", "t": 1, "T": 2_000})
    account.handle_execution_report({"c": "cid-2", "x": "TRADE", "l": "1", "L": "101", "t": 2, "T": 2_100})

    assert account.has_pending_position_update("BTCUSDT") is True
    assert account.has_pending_position_update("ETHUSDT") is True
    await account.handle_account_update({
        "e": "ACCOUNT_UPDATE", "T": 2_000,
        "a": {"P": [{"s": "BTCUSDT", "pa": "-1", "ep": "101", "up": "0", "ps": "BOTH"}]},
    })
    assert account.has_pending_position_update("BTCUSDT") is False
    assert account.has_pending_position_update("ETHUSDT") is True


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


def test_duplicate_trade_report_is_ignored_without_duplicate_commission(tmp_path):
    account, _, wal, _ = _account(tmp_path)
    intent_record = wal.record_intent(_intent(), account_id="spike-test", recorded_at=1_000)
    wal.record_exchange_status(
        intent_record, {"status": "FILLED", "orderId": 42}, recorded_at=1_100
    )
    report = {
        "c": "cid-1",
        "x": "TRADE",
        "l": "0.25",
        "L": "101",
        "n": "0.01",
        "N": "USDT",
        "t": 7,
        "T": 1_200,
    }

    first = account.handle_execution_report(report)
    duplicate = account.handle_execution_report(dict(report))

    assert first is not None
    assert duplicate is None
    assert account._commissions["BTCUSDT"] == Decimal("0.01")


def test_distinct_trade_reports_are_each_returned_and_counted(tmp_path):
    account, _, wal, _ = _account(tmp_path)
    intent_record = wal.record_intent(_intent(), account_id="spike-test", recorded_at=1_000)
    wal.record_exchange_status(
        intent_record,
        {"status": "PARTIALLY_FILLED", "orderId": 42},
        recorded_at=1_100,
    )
    base = {
        "c": "cid-1",
        "x": "TRADE",
        "l": "0.25",
        "L": "101",
        "n": "0.01",
        "N": "USDT",
        "T": 1_200,
    }

    first = account.handle_execution_report({**base, "t": 7})
    second = account.handle_execution_report({**base, "t": 8})

    assert first is not None and first.fill_id == "7"
    assert second is not None and second.fill_id == "8"
    assert account._commissions["BTCUSDT"] == Decimal("0.02")


def test_trade_id_zero_is_valid_and_malformed_first_report_does_not_poison_dedup(tmp_path):
    account, _, wal, _ = _account(tmp_path)
    intent_record = wal.record_intent(_intent(), account_id="spike-test", recorded_at=1_000)
    wal.record_exchange_status(
        intent_record, {"status": "PARTIALLY_FILLED", "orderId": 42}, recorded_at=1_100
    )
    report = {
        "c": "cid-1",
        "x": "TRADE",
        "l": "0.25",
        "L": "invalid",
        "n": "0.01",
        "N": "USDT",
        "t": 0,
        "T": 1_200,
    }

    with pytest.raises(ValueError, match="invalid decimal field: L"):
        account.handle_execution_report(report)

    fill = account.handle_execution_report({**report, "L": "101"})
    assert fill is not None and fill.fill_id == "0"
    assert account._commissions["BTCUSDT"] == Decimal("0.01")


@pytest.mark.asyncio
async def test_restored_trade_state_keeps_old_trade_id_idempotent(tmp_path):
    account, _, wal, _ = _account(tmp_path)
    intent_record = wal.record_intent(_intent(), account_id="spike-test", recorded_at=1_000)
    wal.record_exchange_status(
        intent_record, {"status": "FILLED", "orderId": 42}, recorded_at=1_100
    )
    await account.handle_account_update(
        {
            "e": "ACCOUNT_UPDATE",
            "T": 1_200,
            "a": {
                "P": [
                    {
                        "s": "BTCUSDT",
                        "pa": "-1",
                        "ep": "101",
                        "up": "0",
                        "ps": "BOTH",
                    }
                ]
            },
        }
    )
    account.restore_trade_state("BTCUSDT", Decimal("0.01"), {"7"})

    duplicate = account.handle_execution_report(
        {
            "c": "cid-1",
            "x": "TRADE",
            "l": "1",
            "L": "101",
            "n": "0.01",
            "t": 7,
            "T": 1_200,
        }
    )

    assert duplicate is None
    assert account._commissions["BTCUSDT"] == Decimal("0.01")
    assert account.get_position("BTCUSDT").total_commission == Decimal("0.01")


def test_unresolved_intent_keeps_account_fail_closed(tmp_path):
    account, _, wal, _ = _account(tmp_path)
    wal.record_intent(_intent(), account_id="spike-test", recorded_at=1_000)
    assert account.has_unresolved_orders() is True

    record = wal.recover_latest()["cid-1"]
    wal.record_exchange_status(
        record, {"status": "NEW", "orderId": 42}, recorded_at=1_100
    )
    assert account.has_unresolved_orders() is False
