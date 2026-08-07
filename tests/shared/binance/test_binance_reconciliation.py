from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from trading_platform.ledger.binance_reconciliation import (
    BinanceStartupReconciliationError,
    BinanceStartupReconciler,
)
from trading_platform.ledger.db.models import Order, Position


def open_order(**overrides):
    value = {
        "symbol": "BTCUSDT",
        "orderId": 11,
        "clientOrderId": "cid-1",
        "side": "SELL",
        "type": "LIMIT",
        "positionSide": "SHORT",
        "status": "NEW",
        "origQty": "1.5",
        "executedQty": "0",
        "price": "100",
    }
    value.update(overrides)
    return value


def position(**overrides):
    value = {
        "symbol": "BTCUSDT",
        "positionSide": "SHORT",
        "positionAmt": "-1.5",
        "entryPrice": "100.25",
    }
    value.update(overrides)
    return value


def db_for(*, orders=None, positions=None):
    db = AsyncMock()
    orders = list(orders or [])
    positions = list(positions or [])
    db.count_orders.side_effect = lambda **kwargs: sum(
        order.status == kwargs["status"] for order in orders
    )
    db.get_orders.side_effect = lambda **kwargs: [
        order for order in orders if order.status == kwargs["status"]
    ]
    db.count_positions.return_value = len(positions)
    db.get_positions.return_value = positions
    return db


def local_order(**overrides):
    value = dict(
        account_id="account-1",
        strategy_id="spike_short",
        symbol="BTCUSDT",
        order_id="11",
        client_order_id="cid-1",
        side="SELL",
        order_type="LIMIT",
        position_side="SHORT",
        quantity=Decimal("1.5"),
        filled_quantity=Decimal("0"),
        price=Decimal("100"),
        status="NEW",
    )
    value.update(overrides)
    return Order(**value)


def local_position(**overrides):
    value = dict(
        account_id="account-1",
        strategy_id="spike_short",
        symbol="BTCUSDT",
        position_side="SHORT",
        quantity=Decimal("-1.5"),
        entry_price=Decimal("100.25"),
    )
    value.update(overrides)
    return Position(**value)


@pytest.mark.asyncio
async def test_reconciler_allows_matching_account_snapshot():
    rest = AsyncMock()
    rest.get_open_orders.return_value = [open_order()]
    rest.get_position_risk.return_value = [position(), position(positionAmt="0")]
    db = db_for(orders=[local_order()], positions=[local_position()])

    result = await BinanceStartupReconciler(
        rest, db, account_id="account-1", strategy_id="spike_short"
    ).reconcile_once()

    assert result.open_order_count == 1
    assert result.position_count == 1
    rest.get_open_orders.assert_awaited_once_with()
    rest.get_position_risk.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_reconciler_rejects_unmanaged_exchange_order():
    rest = AsyncMock()
    rest.get_open_orders.return_value = [open_order()]
    rest.get_position_risk.return_value = []
    db = db_for()

    with pytest.raises(BinanceStartupReconciliationError, match="open orders"):
        await BinanceStartupReconciler(
            rest, db, account_id="account-1", strategy_id="spike_short"
        ).reconcile_once()


@pytest.mark.asyncio
async def test_reconciler_rejects_position_quantity_mismatch():
    rest = AsyncMock()
    rest.get_open_orders.return_value = []
    rest.get_position_risk.return_value = [position()]
    db = db_for(positions=[local_position(quantity=Decimal("-1"))])

    with pytest.raises(BinanceStartupReconciliationError, match="positions"):
        await BinanceStartupReconciler(
            rest, db, account_id="account-1", strategy_id="spike_short"
        ).reconcile_once()


@pytest.mark.asyncio
async def test_reconciler_wraps_exchange_failures():
    rest = AsyncMock()
    rest.get_open_orders.side_effect = TimeoutError("offline")
    db = db_for()

    with pytest.raises(BinanceStartupReconciliationError, match="query failed"):
        await BinanceStartupReconciler(
            rest, db, account_id="account-1", strategy_id="spike_short"
        ).reconcile_once()


@pytest.mark.asyncio
async def test_reconciler_rejects_malformed_snapshot():
    rest = AsyncMock()
    rest.get_open_orders.return_value = [open_order(price="NaN")]
    rest.get_position_risk.return_value = []
    db = db_for()

    with pytest.raises(BinanceStartupReconciliationError, match="decimal"):
        await BinanceStartupReconciler(
            rest, db, account_id="account-1", strategy_id="spike_short"
        ).reconcile_once()
