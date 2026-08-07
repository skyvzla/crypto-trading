"""Binance Futures 订单回报到账本模型的严格适配。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from trading_platform.ledger.db.models import LedgerDB, Order, Trade
from trading_platform.shared.execution_recovery import OrderWALRecord

_STATUS_MAP = {
    "NEW": "NEW",
    "PARTIALLY_FILLED": "PARTIALLY_FILLED",
    "FILLED": "FILLED",
    "CANCELED": "CANCELLED",
    "EXPIRED": "EXPIRED",
}


class ExecutionReportError(ValueError):
    """回报缺少必需事实或包含未知枚举。"""


@dataclass(frozen=True)
class ParsedExecutionReport:
    order: Order
    trade: Trade | None


def campaign_id_from_wal_record(
    record: OrderWALRecord,
    *,
    account_id: str,
    strategy_id: str,
    symbol: str,
    client_order_id: str,
    side: str,
    order_type: str,
    quantity: Decimal,
    exchange_order_id: str,
) -> str:
    """Return only an explicitly persisted and identity-matched Campaign."""
    if record.account_id != account_id:
        raise ExecutionReportError("WAL account does not match execution report")
    if record.client_order_id != client_order_id:
        raise ExecutionReportError(
            "WAL client order id does not match execution report"
        )
    if (
        record.symbol != symbol
        or record.side != side
        or record.order_type != order_type
    ):
        raise ExecutionReportError("WAL order identity does not match execution report")
    try:
        wal_quantity = Decimal(record.quantity)
    except (InvalidOperation, ValueError) as exc:
        raise ExecutionReportError("invalid WAL quantity") from exc
    if wal_quantity != quantity:
        raise ExecutionReportError("WAL quantity does not match execution report")
    if (
        record.exchange_order_id is not None
        and record.exchange_order_id != exchange_order_id
    ):
        raise ExecutionReportError(
            "WAL exchange order id does not match execution report"
        )
    if record.payload.get("strategy_id") != strategy_id:
        raise ExecutionReportError("WAL strategy does not match execution report")
    campaign_id = record.payload.get("campaign_id")
    if not isinstance(campaign_id, str) or not campaign_id:
        raise ExecutionReportError("owned WAL order has no explicit campaign_id")
    parts = campaign_id.split(":")
    if (
        len(parts) != 3
        or parts[0] != strategy_id
        or parts[1] != symbol
        or not parts[2].isdigit()
    ):
        raise ExecutionReportError("invalid WAL campaign_id")
    return campaign_id


def _decimal(data: dict[str, Any], key: str, *, default: str | None = None) -> Decimal:
    value = data.get(key, default)
    if value is None:
        raise ExecutionReportError(f"missing execution report field: {key}")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ExecutionReportError(f"invalid decimal field: {key}") from exc


def _required(data: dict[str, Any], key: str) -> Any:
    value = data.get(key)
    if value is None or value == "":
        raise ExecutionReportError(f"missing execution report field: {key}")
    return value


def _datetime_ms(value: Any, field: str) -> datetime:
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OSError) as exc:
        raise ExecutionReportError(f"invalid timestamp field: {field}") from exc


def parse_execution_report(
    order_data: dict[str, Any],
    *,
    account_id: str,
    strategy_id: str,
    wal_record: OrderWALRecord | None = None,
) -> ParsedExecutionReport:
    """解析 `ORDER_TRADE_UPDATE.o`；账户和策略归属必须显式提供。"""
    if not account_id or not strategy_id:
        raise ExecutionReportError("account_id and strategy_id are required")

    status_raw = str(_required(order_data, "X"))
    status = _STATUS_MAP.get(status_raw)
    if status is None:
        raise ExecutionReportError(f"unknown Binance order status: {status_raw}")
    side = str(_required(order_data, "S"))
    if side not in {"BUY", "SELL"}:
        raise ExecutionReportError(f"unknown Binance order side: {side}")

    symbol = str(_required(order_data, "s"))
    exchange_order_id = str(_required(order_data, "i"))
    client_order_id = str(_required(order_data, "c"))
    event_time = _required(order_data, "T")
    price = _decimal(order_data, "p", default="0")
    stop_price = _decimal(order_data, "sp", default="0")
    avg_fill_price = _decimal(order_data, "ap", default="0")

    order = Order(
        account_id=account_id,
        strategy_id=strategy_id,
        symbol=symbol,
        order_id=exchange_order_id,
        client_order_id=client_order_id,
        side=side,
        order_type=str(_required(order_data, "o")),
        position_side=order_data.get("ps"),
        quantity=_decimal(order_data, "q"),
        price=price if price > 0 else None,
        stop_price=stop_price if stop_price > 0 else None,
        status=status,
        filled_quantity=_decimal(order_data, "z", default="0"),
        avg_fill_price=avg_fill_price if avg_fill_price > 0 else None,
        exchange_created_at=_datetime_ms(order_data.get("O", event_time), "O"),
    )
    if wal_record is not None:
        order.campaign_id = campaign_id_from_wal_record(
            wal_record,
            account_id=account_id,
            strategy_id=strategy_id,
            symbol=symbol,
            client_order_id=client_order_id,
            side=side,
            order_type=order.order_type,
            quantity=order.quantity,
            exchange_order_id=exchange_order_id,
        )

    execution_type = str(order_data.get("x", ""))
    trade_id = order_data.get("t")
    last_quantity = _decimal(order_data, "l", default="0")
    if execution_type != "TRADE" or trade_id in (None, -1, "-1") or last_quantity <= 0:
        return ParsedExecutionReport(order=order, trade=None)

    last_price = _decimal(order_data, "L")
    quote_quantity = _decimal(
        order_data,
        "Y",
        default=str(last_quantity * last_price),
    )
    commission_value = order_data.get("n")
    commission = (
        Decimal("0")
        if commission_value in (None, "")
        else _decimal(order_data, "n")
    )
    realized_pnl_value = order_data.get("rp")
    realized_pnl = (
        None if realized_pnl_value in (None, "") else _decimal(order_data, "rp")
    )
    trade = Trade(
        account_id=account_id,
        strategy_id=strategy_id,
        symbol=symbol,
        trade_id=str(trade_id),
        order_id=exchange_order_id,
        client_order_id=client_order_id,
        side=side,
        position_side=order_data.get("ps"),
        quantity=last_quantity,
        price=last_price,
        quote_quantity=quote_quantity,
        commission=commission,
        commission_asset=str(order_data.get("N") or ""),
        realized_pnl=realized_pnl,
        is_maker=bool(order_data.get("m", False)),
        exchange_time=_datetime_ms(event_time, "T"),
        campaign_id=order.campaign_id,
    )
    return ParsedExecutionReport(order=order, trade=trade)


class BinanceExecutionReportLedger:
    """将解析成功的订单回报原子写入 PostgreSQL 账本。"""

    def __init__(self, db: LedgerDB, *, account_id: str, strategy_id: str):
        if not account_id or not strategy_id:
            raise ValueError("account_id and strategy_id are required")
        self.db = db
        self.account_id = account_id
        self.strategy_id = strategy_id

    async def handle(
        self,
        order_data: dict[str, Any],
        wal_record: OrderWALRecord,
    ) -> tuple[int, int | None]:
        report = parse_execution_report(
            order_data,
            account_id=self.account_id,
            strategy_id=self.strategy_id,
            wal_record=wal_record,
        )
        return await self.db.apply_execution_report(report.order, report.trade)
