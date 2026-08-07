from decimal import Decimal
from unittest.mock import AsyncMock, Mock

import pytest

from trading_platform.ledger.binance_startup_sync import (
    BinanceRecoverThenReconcile,
    BinanceStartupSyncError,
    BinanceStartupSynchronizer,
)
from trading_platform.shared.binance.live_executor import BinanceOrderExecutor
from trading_platform.shared.events import OrderIntent
from trading_platform.shared.execution_recovery import OrderWAL


ACCOUNT_ID = "account-1"
STRATEGY_ID = "spike_short"
SYMBOL = "BTCUSDT"
CLIENT_ORDER_ID = "cid-startup-1"
ORDER_ID = 42
RECORDED_AT = 1_700_000_000_000


def query_order(**overrides):
    value = {
        "symbol": SYMBOL,
        "orderId": ORDER_ID,
        "clientOrderId": CLIENT_ORDER_ID,
        "side": "SELL",
        "type": "LIMIT",
        "positionSide": "BOTH",
        "status": "FILLED",
        "origQty": "0.1",
        "executedQty": "0.1",
        "price": "100",
        "stopPrice": "0",
        "avgPrice": "100",
        "time": RECORDED_AT + 100,
    }
    value.update(overrides)
    return value


def account_trade(**overrides):
    value = {
        "symbol": SYMBOL,
        "id": 501,
        "orderId": ORDER_ID,
        "side": "SELL",
        "positionSide": "BOTH",
        "qty": "0.1",
        "price": "100",
        "quoteQty": "10",
        "commission": "0.004",
        "commissionAsset": "USDT",
        "realizedPnl": "0",
        "maker": True,
        "time": RECORDED_AT + 200,
    }
    value.update(overrides)
    return value


def position(**overrides):
    value = {
        "symbol": SYMBOL,
        "positionSide": "BOTH",
        "positionAmt": "-0.1",
        "entryPrice": "100",
        "markPrice": "99",
        "unRealizedProfit": "0.1",
        "liquidationPrice": "500",
        "leverage": "1",
        "marginType": "cross",
        "isolatedMargin": "0",
        "updateTime": RECORDED_AT + 300,
    }
    value.update(overrides)
    return value


def make_sync(tmp_path, *, order_response=None, trades=None, positions=None):
    wal = OrderWAL(tmp_path / "orders.jsonl")
    intent = wal.record_intent(
        OrderIntent(
            symbol=SYMBOL,
            side="SELL",
            price=Decimal("100"),
            quantity=Decimal("0.1"),
            client_order_id=CLIENT_ORDER_ID,
            strategy_id=STRATEGY_ID,
        ),
        account_id=ACCOUNT_ID,
        recorded_at=RECORDED_AT,
    )
    wal.record_exchange_status(
        intent,
        {"status": "NEW", "orderId": ORDER_ID},
        recorded_at=RECORDED_AT + 1,
    )
    rest = Mock(
        query_order=AsyncMock(
            return_value=query_order() if order_response is None else order_response
        ),
        get_account_trades=AsyncMock(
            return_value=[account_trade()] if trades is None else trades
        ),
        get_position_risk=AsyncMock(
            return_value=[position()] if positions is None else positions
        ),
    )
    db = Mock(
        insert_order=AsyncMock(return_value=1),
        insert_trade=AsyncMock(return_value=1),
        upsert_position=AsyncMock(return_value=1),
    )
    executor = BinanceOrderExecutor(
        rest,
        wal,
        account_id=ACCOUNT_ID,
        now_ms=lambda: RECORDED_AT + 400,
    )
    synchronizer = BinanceStartupSynchronizer(
        rest,
        executor,
        db,
        account_id=ACCOUNT_ID,
        strategy_id=STRATEGY_ID,
        symbols=[SYMBOL],
        now_ms=lambda: RECORDED_AT + 1_000,
    )
    return synchronizer, rest, db, wal


@pytest.mark.asyncio
async def test_wal_new_to_filled_backfills_order_trade_position_before_strict(tmp_path):
    synchronizer, rest, db, wal = make_sync(tmp_path)
    events = []
    db.insert_order.side_effect = lambda value: events.append(("order", value)) or 1
    db.insert_trade.side_effect = lambda value: events.append(("trade", value)) or 1
    db.upsert_position.side_effect = lambda value: events.append(("position", value)) or 1
    strict = Mock(
        reconcile_once=AsyncMock(
            side_effect=lambda: events.append(("strict", None)) or "ok"
        )
    )

    result = await BinanceRecoverThenReconcile(synchronizer, strict).reconcile_once()

    assert result == "ok"
    assert [kind for kind, _ in events] == ["order", "trade", "position", "strict"]
    assert events[0][1].status == "FILLED"
    assert events[1][1].client_order_id == CLIENT_ORDER_ID
    assert events[2][1].quantity == Decimal("-0.1")
    assert wal.recover_latest()[CLIENT_ORDER_ID].status == "FILLED"
    rest.query_order.assert_awaited_once_with(
        SYMBOL, orig_client_order_id=CLIENT_ORDER_ID
    )


@pytest.mark.asyncio
async def test_unacknowledged_filled_wal_backfills_missing_order_and_trade_once(tmp_path):
    synchronizer, rest, db, wal = make_sync(tmp_path)
    wal.record_exchange_status(
        wal.recover_latest()[CLIENT_ORDER_ID],
        query_order(),
        recorded_at=RECORDED_AT + 2,
    )

    first = await synchronizer.sync_once()

    assert first.order_count == 1
    assert first.trade_count == 1
    assert db.insert_order.await_args.args[0].status == "FILLED"
    assert db.insert_trade.await_args.args[0].client_order_id == CLIENT_ORDER_ID
    acknowledged = wal.recover_latest()[CLIENT_ORDER_ID]
    assert wal.ledger_acknowledged(acknowledged)

    rest.query_order.reset_mock()
    rest.get_account_trades.reset_mock()
    db.insert_order.reset_mock()
    db.insert_trade.reset_mock()

    second = await synchronizer.sync_once()

    assert second.order_count == 0
    assert second.trade_count == 0
    rest.query_order.assert_not_awaited()
    rest.get_account_trades.assert_not_awaited()
    db.insert_order.assert_not_awaited()
    db.insert_trade.assert_not_awaited()


@pytest.mark.asyncio
async def test_unacknowledged_cancelled_wal_repairs_stale_new_ledger_order(tmp_path):
    cancelled = query_order(
        status="CANCELED",
        executedQty="0",
        avgPrice="0",
    )
    synchronizer, _, db, wal = make_sync(
        tmp_path,
        order_response=cancelled,
        trades=[],
    )
    wal.record_exchange_status(
        wal.recover_latest()[CLIENT_ORDER_ID],
        cancelled,
        recorded_at=RECORDED_AT + 2,
    )

    result = await synchronizer.sync_once()

    assert result.order_count == 1
    assert result.trade_count == 0
    repaired = db.insert_order.await_args.args[0]
    assert repaired.status == "CANCELLED"
    assert repaired.filled_quantity == Decimal("0")
    assert wal.ledger_acknowledged(wal.recover_latest()[CLIENT_ORDER_ID])


@pytest.mark.asyncio
async def test_terminal_wal_is_not_acknowledged_when_position_sync_fails(tmp_path):
    synchronizer, _, _, wal = make_sync(tmp_path, positions=[])
    wal.record_exchange_status(
        wal.recover_latest()[CLIENT_ORDER_ID],
        query_order(),
        recorded_at=RECORDED_AT + 5_000,
    )

    with pytest.raises(BinanceStartupSyncError, match="position snapshot missing"):
        await synchronizer.sync_once()

    assert not wal.ledger_acknowledged(wal.recover_latest()[CLIENT_ORDER_ID])


@pytest.mark.asyncio
async def test_terminal_trade_recovery_starts_from_immutable_intent_time(tmp_path):
    synchronizer, rest, _, wal = make_sync(tmp_path)
    wal.record_exchange_status(
        wal.recover_latest()[CLIENT_ORDER_ID],
        query_order(),
        recorded_at=RECORDED_AT + 60_000,
    )

    await synchronizer.sync_once()

    assert rest.get_account_trades.await_args.kwargs["start_time"] == RECORDED_AT - 1_000


@pytest.mark.asyncio
async def test_query_order_none_fails_before_trade_position_or_strict(tmp_path):
    synchronizer, rest, db, _ = make_sync(tmp_path, order_response=None)
    rest.query_order.return_value = None
    strict = Mock(reconcile_once=AsyncMock())

    with pytest.raises(BinanceStartupSyncError, match="missing from exchange"):
        await BinanceRecoverThenReconcile(synchronizer, strict).reconcile_once()

    rest.get_account_trades.assert_not_awaited()
    rest.get_position_risk.assert_not_awaited()
    db.insert_order.assert_not_awaited()
    strict.reconcile_once.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mismatch",
    [
        {"clientOrderId": "foreign-client-id"},
        {"symbol": "ETHUSDT"},
        {"side": "BUY"},
        {"type": "MARKET"},
        {"origQty": "0.2"},
        {"orderId": 99},
    ],
)
async def test_owned_order_identity_mismatch_fails_closed(tmp_path, mismatch):
    synchronizer, rest, db, _ = make_sync(
        tmp_path, order_response=query_order(**mismatch)
    )

    with pytest.raises(BinanceStartupSyncError, match="identity mismatch"):
        await synchronizer.sync_once()

    db.insert_order.assert_not_awaited()
    rest.get_account_trades.assert_not_awaited()


@pytest.mark.asyncio
async def test_recovered_trade_quantity_must_equal_exchange_executed_quantity(tmp_path):
    synchronizer, rest, db, _ = make_sync(
        tmp_path,
        trades=[account_trade(qty="0.04", quoteQty="4")],
    )

    with pytest.raises(BinanceStartupSyncError, match="trade quantity mismatch"):
        await synchronizer.sync_once()

    db.insert_order.assert_awaited_once()
    db.insert_trade.assert_awaited_once()
    rest.get_position_risk.assert_not_awaited()


@pytest.mark.asyncio
async def test_exactly_1000_account_trades_requires_pagination(tmp_path):
    trades = [account_trade(id=index, orderId=9000 + index) for index in range(1000)]
    synchronizer, rest, db, _ = make_sync(tmp_path, trades=trades)

    with pytest.raises(BinanceStartupSyncError, match="requires pagination"):
        await synchronizer.sync_once()

    db.insert_trade.assert_not_awaited()
    rest.get_position_risk.assert_not_awaited()


@pytest.mark.asyncio
async def test_hedge_position_snapshot_fails_closed(tmp_path):
    synchronizer, _, db, _ = make_sync(
        tmp_path,
        positions=[position(positionSide="SHORT")],
    )

    with pytest.raises(BinanceStartupSyncError, match="not one-way mode"):
        await synchronizer.sync_once()

    db.upsert_position.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "trade_override",
    [
        {"side": "BUY"},
        {"positionSide": "SHORT"},
    ],
)
async def test_owned_trade_identity_mismatch_fails_closed(
    tmp_path, trade_override
):
    synchronizer, rest, db, _ = make_sync(
        tmp_path,
        trades=[account_trade(**trade_override)],
    )

    with pytest.raises(BinanceStartupSyncError, match="trade identity mismatch"):
        await synchronizer.sync_once()

    db.insert_trade.assert_not_awaited()
    rest.get_position_risk.assert_not_awaited()


@pytest.mark.asyncio
async def test_invalid_position_leverage_is_wrapped_as_startup_sync_error(tmp_path):
    synchronizer, _, db, _ = make_sync(
        tmp_path,
        positions=[position(leverage="not-an-integer")],
    )

    with pytest.raises(BinanceStartupSyncError, match="position leverage"):
        await synchronizer.sync_once()

    db.upsert_position.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_position_snapshot_fails_closed(tmp_path):
    synchronizer, _, db, _ = make_sync(
        tmp_path,
        positions=[position(), position(positionAmt="0")],
    )

    with pytest.raises(BinanceStartupSyncError, match="duplicate configured"):
        await synchronizer.sync_once()

    db.upsert_position.assert_not_awaited()


@pytest.mark.asyncio
async def test_foreign_account_trades_are_not_written_or_counted(tmp_path):
    foreign = account_trade(id=777, symbol="ETHUSDT", orderId=ORDER_ID)
    owned = account_trade(id=501, orderId=ORDER_ID)
    synchronizer, _, db, _ = make_sync(tmp_path, trades=[foreign, owned])

    result = await synchronizer.sync_once()

    assert result.trade_count == 1
    db.insert_trade.assert_awaited_once()
    assert db.insert_trade.await_args.args[0].trade_id == "501"


@pytest.mark.asyncio
async def test_strict_reconcile_never_runs_when_sync_fails():
    order = []
    synchronizer = Mock(
        sync_once=AsyncMock(
            side_effect=lambda: order.append("sync")
            or (_ for _ in ()).throw(BinanceStartupSyncError("incomplete"))
        )
    )
    strict = Mock(
        reconcile_once=AsyncMock(side_effect=lambda: order.append("strict"))
    )

    with pytest.raises(BinanceStartupSyncError, match="incomplete"):
        await BinanceRecoverThenReconcile(synchronizer, strict).reconcile_once()

    assert order == ["sync"]
    strict.reconcile_once.assert_not_awaited()


@pytest.mark.asyncio
async def test_sync_completes_before_strict_reconcile():
    order = []
    synchronizer = Mock(
        sync_once=AsyncMock(side_effect=lambda: order.append("sync"))
    )
    strict = Mock(
        reconcile_once=AsyncMock(side_effect=lambda: order.append("strict") or "done")
    )

    result = await BinanceRecoverThenReconcile(synchronizer, strict).reconcile_once()

    assert result == "done"
    assert order == ["sync", "strict"]
