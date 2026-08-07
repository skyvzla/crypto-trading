"""从 Binance REST 恢复进程离线期间错过的活跃交易事实。"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Protocol

from trading_platform.ledger.db.models import LedgerDB, Order, Position, Trade
from trading_platform.shared.binance.live_executor import BinanceOrderExecutor
from trading_platform.shared.binance.rest_client import BinanceRestClient


_STATUS_MAP = {
    "NEW": "NEW",
    "PARTIALLY_FILLED": "PARTIALLY_FILLED",
    "FILLED": "FILLED",
    "CANCELED": "CANCELLED",
    "EXPIRED": "EXPIRED",
}
_ACTIVE_WAL_STATUSES = {None, "NEW", "PARTIALLY_FILLED", "SUBMIT_UNKNOWN"}
_TERMINAL_WAL_STATUSES = {"FILLED", "CANCELLED", "EXPIRED"}
_MAX_TRADE_WINDOW_MS = 7 * 24 * 60 * 60 * 1000 - 1


class BinanceStartupSyncError(RuntimeError):
    """交易所恢复事实缺失、超量或与 WAL 身份不一致。"""


class StrictReconciler(Protocol):
    async def reconcile_once(self) -> object:
        ...


@dataclass(frozen=True)
class BinanceStartupSyncResult:
    order_count: int
    trade_count: int
    position_count: int


def _required(data: dict[str, Any], key: str, *, fact: str) -> Any:
    value = data.get(key)
    if value is None or value == "":
        raise BinanceStartupSyncError(f"missing Binance {fact} field: {key}")
    return value


def _decimal(
    data: dict[str, Any], key: str, *, fact: str, default: str | None = None
) -> Decimal:
    raw = data.get(key, default)
    if raw is None:
        raise BinanceStartupSyncError(f"missing Binance {fact} field: {key}")
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError) as exc:
        raise BinanceStartupSyncError(f"invalid Binance {fact} decimal: {key}") from exc
    if not value.is_finite():
        raise BinanceStartupSyncError(f"invalid Binance {fact} decimal: {key}")
    return value


def _datetime_ms(value: Any, *, fact: str, field: str) -> datetime:
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OSError) as exc:
        raise BinanceStartupSyncError(
            f"invalid Binance {fact} timestamp: {field}"
        ) from exc


def parse_query_order(
    raw: dict[str, Any], *, account_id: str, strategy_id: str
) -> Order:
    status_raw = str(_required(raw, "status", fact="query order"))
    status = _STATUS_MAP.get(status_raw)
    if status is None:
        raise BinanceStartupSyncError(f"unknown Binance query order status: {status_raw}")
    price = _decimal(raw, "price", fact="query order", default="0")
    stop_price = _decimal(raw, "stopPrice", fact="query order", default="0")
    average = _decimal(raw, "avgPrice", fact="query order", default="0")
    return Order(
        account_id=account_id,
        strategy_id=strategy_id,
        symbol=str(_required(raw, "symbol", fact="query order")),
        order_id=str(_required(raw, "orderId", fact="query order")),
        client_order_id=str(_required(raw, "clientOrderId", fact="query order")),
        side=str(_required(raw, "side", fact="query order")),
        order_type=str(_required(raw, "type", fact="query order")),
        position_side=str(raw.get("positionSide") or "BOTH"),
        quantity=_decimal(raw, "origQty", fact="query order"),
        price=price if price > 0 else None,
        stop_price=stop_price if stop_price > 0 else None,
        status=status,
        filled_quantity=_decimal(raw, "executedQty", fact="query order", default="0"),
        avg_fill_price=average if average > 0 else None,
        exchange_created_at=_datetime_ms(
            _required(raw, "time", fact="query order"),
            fact="query order",
            field="time",
        ),
    )


def parse_account_trade(
    raw: dict[str, Any],
    *,
    account_id: str,
    strategy_id: str,
    client_order_id: str,
) -> Trade:
    quantity = _decimal(raw, "qty", fact="account trade")
    price = _decimal(raw, "price", fact="account trade")
    return Trade(
        account_id=account_id,
        strategy_id=strategy_id,
        symbol=str(_required(raw, "symbol", fact="account trade")),
        trade_id=str(_required(raw, "id", fact="account trade")),
        order_id=str(_required(raw, "orderId", fact="account trade")),
        client_order_id=client_order_id,
        side=str(_required(raw, "side", fact="account trade")),
        position_side=str(raw.get("positionSide") or "BOTH"),
        quantity=quantity,
        price=price,
        quote_quantity=_decimal(
            raw,
            "quoteQty",
            fact="account trade",
            default=str(quantity * price),
        ),
        commission=_decimal(raw, "commission", fact="account trade", default="0"),
        commission_asset=str(raw.get("commissionAsset") or ""),
        realized_pnl=_decimal(raw, "realizedPnl", fact="account trade", default="0"),
        is_maker=bool(raw.get("maker", False)),
        exchange_time=_datetime_ms(
            _required(raw, "time", fact="account trade"),
            fact="account trade",
            field="time",
        ),
    )


def parse_position_snapshot(
    raw: dict[str, Any], *, account_id: str, strategy_id: str
) -> Position:
    position_side = str(raw.get("positionSide") or "BOTH")
    if position_side != "BOTH":
        raise BinanceStartupSyncError("startup position snapshot is not one-way mode")
    try:
        leverage = int(_required(raw, "leverage", fact="position"))
    except (TypeError, ValueError) as exc:
        raise BinanceStartupSyncError("invalid Binance position leverage") from exc
    return Position(
        account_id=account_id,
        strategy_id=strategy_id,
        symbol=str(_required(raw, "symbol", fact="position")),
        position_side=position_side,
        quantity=_decimal(raw, "positionAmt", fact="position"),
        entry_price=_decimal(raw, "entryPrice", fact="position"),
        mark_price=_decimal(raw, "markPrice", fact="position"),
        unrealized_pnl=_decimal(raw, "unRealizedProfit", fact="position"),
        liquidation_price=_decimal(raw, "liquidationPrice", fact="position"),
        leverage=leverage,
        margin_type=str(_required(raw, "marginType", fact="position")),
        isolated_margin=_decimal(raw, "isolatedMargin", fact="position"),
        exchange_time=_datetime_ms(
            _required(raw, "updateTime", fact="position"),
            fact="position",
            field="updateTime",
        ),
    )


class BinanceStartupSynchronizer:
    """回补活跃订单和未确认的终态 WAL，再同步仓位快照。"""

    def __init__(
        self,
        rest_client: BinanceRestClient,
        executor: BinanceOrderExecutor,
        db: LedgerDB,
        *,
        account_id: str,
        strategy_id: str,
        symbols: list[str],
        now_ms: Callable[[], int] | None = None,
    ):
        if not account_id or not strategy_id or not symbols:
            raise ValueError("account_id, strategy_id and symbols are required")
        self.rest_client = rest_client
        self.executor = executor
        self.db = db
        self.account_id = account_id
        self.strategy_id = strategy_id
        self.symbols = tuple(dict.fromkeys(symbols))
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))

    async def sync_once(self) -> BinanceStartupSyncResult:
        latest = self.executor.wal.recover_latest()
        ledger_acknowledgements = (
            self.executor.wal.recover_ledger_acknowledgements()
        )
        records = [
            record
            for record in latest.values()
            if record.account_id == self.account_id
            and record.symbol in self.symbols
            and (
                record.record_type == "intent"
                or record.status in _ACTIVE_WAL_STATUSES
                or (
                    record.status in _TERMINAL_WAL_STATUSES
                    and ledger_acknowledgements.get(record.client_order_id) != {
                        "recorded_at": record.recorded_at,
                        "status": record.status,
                        "exchange_order_id": record.exchange_order_id,
                    }
                )
            )
        ]
        recovered_orders: dict[tuple[str, str], Order] = {}
        recovered_records = {}
        earliest_by_symbol: dict[str, int] = {}
        for record in records:
            response = await self.rest_client.query_order(
                record.symbol, orig_client_order_id=record.client_order_id
            )
            if response is None:
                raise BinanceStartupSyncError(
                    f"owned active order missing from exchange: {record.client_order_id}"
                )
            try:
                recovered_record = self.executor.reconcile_order_response(response)
            except Exception as exc:
                raise BinanceStartupSyncError(
                    f"owned order identity mismatch: {record.client_order_id}"
                ) from exc
            order = parse_query_order(
                response,
                account_id=self.account_id,
                strategy_id=self.strategy_id,
            )
            await self.db.insert_order(order)
            recovered_orders[(order.symbol, order.order_id)] = order
            recovered_records[order.client_order_id] = recovered_record
            earliest_by_symbol[record.symbol] = min(
                earliest_by_symbol.get(
                    record.symbol,
                    record.intent_created_at or record.recorded_at,
                ),
                record.intent_created_at or record.recorded_at,
            )

        trade_count = 0
        trades_by_order: dict[tuple[str, str], Decimal] = {}
        now_ms = self._now_ms()
        for symbol, start_ms in earliest_by_symbol.items():
            cursor = max(0, start_ms - 1000)
            while cursor <= now_ms:
                end_ms = min(now_ms, cursor + _MAX_TRADE_WINDOW_MS)
                trades = await self.rest_client.get_account_trades(
                    symbol,
                    start_time=cursor,
                    end_time=end_ms,
                    limit=1000,
                )
                if len(trades) == 1000:
                    raise BinanceStartupSyncError(
                        f"account trade recovery requires pagination: {symbol}"
                    )
                for raw in trades:
                    order_key = (str(raw.get("symbol")), str(raw.get("orderId")))
                    order = recovered_orders.get(order_key)
                    if order is None:
                        continue
                    trade = parse_account_trade(
                        raw,
                        account_id=self.account_id,
                        strategy_id=self.strategy_id,
                        client_order_id=order.client_order_id,
                    )
                    if (
                        trade.symbol != order.symbol
                        or trade.side != order.side
                        or trade.position_side != order.position_side
                    ):
                        raise BinanceStartupSyncError(
                            f"recovered trade identity mismatch for order: "
                            f"{order.client_order_id}"
                        )
                    await self.db.insert_trade(trade)
                    trades_by_order[order_key] = (
                        trades_by_order.get(order_key, Decimal("0")) + trade.quantity
                    )
                    trade_count += 1
                cursor = end_ms + 1

        for order_key, order in recovered_orders.items():
            if trades_by_order.get(order_key, Decimal("0")) != order.filled_quantity:
                raise BinanceStartupSyncError(
                    f"recovered trade quantity mismatch for order: {order.client_order_id}"
                )

        snapshots = await self.rest_client.get_position_risk()
        if not isinstance(snapshots, list):
            raise BinanceStartupSyncError("invalid Binance position snapshot")
        selected = [raw for raw in snapshots if raw.get("symbol") in self.symbols]
        selected_symbols = [str(raw.get("symbol")) for raw in selected]
        if set(selected_symbols) != set(self.symbols):
            raise BinanceStartupSyncError("position snapshot missing configured symbol")
        if len(selected_symbols) != len(set(selected_symbols)):
            raise BinanceStartupSyncError("duplicate configured position snapshot")
        for raw in selected:
            await self.db.upsert_position(
                parse_position_snapshot(
                    raw,
                    account_id=self.account_id,
                    strategy_id=self.strategy_id,
                )
            )
        for record in recovered_records.values():
            if record.status in _TERMINAL_WAL_STATUSES:
                self.executor.wal.acknowledge_ledger(record)
        return BinanceStartupSyncResult(
            order_count=len(recovered_orders),
            trade_count=trade_count,
            position_count=len(selected),
        )


class BinanceRecoverThenReconcile:
    """先幂等回补，再执行原有严格快照相等门禁。"""

    def __init__(
        self,
        synchronizer: BinanceStartupSynchronizer,
        strict_reconciler: StrictReconciler,
    ):
        self.synchronizer = synchronizer
        self.strict_reconciler = strict_reconciler

    async def reconcile_once(self) -> object:
        await self.synchronizer.sync_once()
        return await self.strict_reconciler.reconcile_once()
