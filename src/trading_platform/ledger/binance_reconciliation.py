"""Binance 交易所快照与本地账本的启动一致性门禁。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from trading_platform.ledger.db.models import LedgerDB, Order, Position
from trading_platform.shared.binance.rest_client import BinanceRestClient


class BinanceStartupReconciliationError(RuntimeError):
    """交易所事实无法与指定账户、策略账本对齐。"""


@dataclass(frozen=True, order=True)
class _OpenOrderFact:
    symbol: str
    order_id: str
    client_order_id: str
    side: str
    order_type: str
    position_side: str
    status: str
    quantity: Decimal
    filled_quantity: Decimal
    price: Decimal


@dataclass(frozen=True, order=True)
class _PositionFact:
    symbol: str
    position_side: str
    quantity: Decimal
    entry_price: Decimal


@dataclass(frozen=True)
class BinanceReconciliationResult:
    open_order_count: int
    position_count: int


def _required(data: dict[str, Any], key: str, *, fact: str) -> Any:
    value = data.get(key)
    if value is None or value == "":
        raise BinanceStartupReconciliationError(
            f"missing Binance {fact} field: {key}"
        )
    return value


def _decimal(data: dict[str, Any], key: str, *, fact: str) -> Decimal:
    try:
        value = Decimal(str(_required(data, key, fact=fact)))
    except (InvalidOperation, ValueError) as exc:
        raise BinanceStartupReconciliationError(
            f"invalid Binance {fact} decimal field: {key}"
        ) from exc
    if not value.is_finite():
        raise BinanceStartupReconciliationError(
            f"invalid Binance {fact} decimal field: {key}"
        )
    return value


def _exchange_order_fact(data: dict[str, Any]) -> _OpenOrderFact:
    fact = "open order"
    status = str(_required(data, "status", fact=fact))
    if status not in {"NEW", "PARTIALLY_FILLED"}:
        raise BinanceStartupReconciliationError(
            f"unexpected Binance open order status: {status}"
        )
    side = str(_required(data, "side", fact=fact))
    if side not in {"BUY", "SELL"}:
        raise BinanceStartupReconciliationError(
            f"unexpected Binance open order side: {side}"
        )
    return _OpenOrderFact(
        symbol=str(_required(data, "symbol", fact=fact)),
        order_id=str(_required(data, "orderId", fact=fact)),
        client_order_id=str(_required(data, "clientOrderId", fact=fact)),
        side=side,
        order_type=str(_required(data, "type", fact=fact)),
        position_side=str(data.get("positionSide") or "BOTH"),
        status=status,
        quantity=_decimal(data, "origQty", fact=fact),
        filled_quantity=_decimal(data, "executedQty", fact=fact),
        price=_decimal(data, "price", fact=fact),
    )


def _ledger_order_fact(order: Order) -> _OpenOrderFact:
    return _OpenOrderFact(
        symbol=order.symbol,
        order_id=order.order_id,
        client_order_id=order.client_order_id,
        side=order.side,
        order_type=order.order_type,
        position_side=order.position_side or "BOTH",
        status=order.status,
        quantity=order.quantity,
        filled_quantity=order.filled_quantity,
        price=order.price or Decimal("0"),
    )


def _exchange_position_fact(data: dict[str, Any]) -> _PositionFact:
    fact = "position"
    return _PositionFact(
        symbol=str(_required(data, "symbol", fact=fact)),
        position_side=str(data.get("positionSide") or "BOTH"),
        quantity=_decimal(data, "positionAmt", fact=fact),
        entry_price=_decimal(data, "entryPrice", fact=fact),
    )


def _ledger_position_fact(position: Position) -> _PositionFact:
    return _PositionFact(
        symbol=position.symbol,
        position_side=position.position_side,
        quantity=position.quantity,
        entry_price=position.entry_price,
    )


def _unique_facts(values: list[Any], *, source: str) -> set[Any]:
    facts = set(values)
    if len(facts) != len(values):
        raise BinanceStartupReconciliationError(
            f"duplicate {source} facts prevent startup reconciliation"
        )
    return facts


def _difference_summary(exchange: set[Any], ledger: set[Any]) -> str:
    exchange_only = sorted(exchange - ledger)[:3]
    ledger_only = sorted(ledger - exchange)[:3]
    return f"exchange_only={exchange_only!r}, ledger_only={ledger_only!r}"


class BinanceStartupReconciler:
    """验证专用 Binance 账户的开放风险已完整反映在本地账本中。"""

    def __init__(
        self,
        rest_client: BinanceRestClient,
        db: LedgerDB,
        *,
        account_id: str,
        strategy_id: str,
    ):
        if not account_id or not strategy_id:
            raise ValueError("account_id and strategy_id are required")
        self.rest_client = rest_client
        self.db = db
        self.account_id = account_id
        self.strategy_id = strategy_id

    async def reconcile_once(self) -> BinanceReconciliationResult:
        """比较账户级交易所快照；任何不完整或不一致都拒绝启动。"""
        try:
            exchange_orders_raw = await self.rest_client.get_open_orders()
            exchange_positions_raw = await self.rest_client.get_position_risk()
            ledger_orders = await self._get_open_orders()
            ledger_positions = await self._get_positions()
        except BinanceStartupReconciliationError:
            raise
        except Exception as exc:
            raise BinanceStartupReconciliationError(
                f"Binance startup reconciliation query failed: {type(exc).__name__}"
            ) from exc

        if not isinstance(exchange_orders_raw, list):
            raise BinanceStartupReconciliationError(
                "invalid Binance open orders response"
            )
        if not isinstance(exchange_positions_raw, list):
            raise BinanceStartupReconciliationError(
                "invalid Binance position response"
            )

        exchange_orders = _unique_facts(
            [_exchange_order_fact(value) for value in exchange_orders_raw],
            source="Binance open order",
        )
        local_orders = _unique_facts(
            [_ledger_order_fact(value) for value in ledger_orders],
            source="ledger open order",
        )
        if exchange_orders != local_orders:
            raise BinanceStartupReconciliationError(
                "Binance open orders do not match the dedicated strategy ledger: "
                + _difference_summary(exchange_orders, local_orders)
            )

        exchange_position_values = [
            _exchange_position_fact(value) for value in exchange_positions_raw
        ]
        exchange_positions = _unique_facts(
            [value for value in exchange_position_values if value.quantity != 0],
            source="Binance nonzero position",
        )
        local_positions = _unique_facts(
            [_ledger_position_fact(value) for value in ledger_positions],
            source="ledger nonzero position",
        )
        if exchange_positions != local_positions:
            raise BinanceStartupReconciliationError(
                "Binance positions do not match the dedicated strategy ledger: "
                + _difference_summary(exchange_positions, local_positions)
            )

        return BinanceReconciliationResult(
            open_order_count=len(exchange_orders),
            position_count=len(exchange_positions),
        )

    async def _get_open_orders(self) -> list[Order]:
        orders: list[Order] = []
        for status in ("NEW", "PARTIALLY_FILLED"):
            count = await self.db.count_orders(
                account_id=self.account_id,
                strategy_id=self.strategy_id,
                status=status,
            )
            if count:
                orders.extend(
                    await self.db.get_orders(
                        account_id=self.account_id,
                        strategy_id=self.strategy_id,
                        status=status,
                        limit=count,
                    )
                )
        return orders

    async def _get_positions(self) -> list[Position]:
        count = await self.db.count_positions(
            account_id=self.account_id,
            strategy_id=self.strategy_id,
        )
        if not count:
            return []
        return await self.db.get_positions(
            account_id=self.account_id,
            strategy_id=self.strategy_id,
            limit=count,
        )
