"""Binance Futures 账户更新到账本仓位模型的严格适配。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from trading_platform.ledger.db.models import LedgerDB, Position

_ACCOUNT_UPDATE_REASONS = {
    "ADJUSTMENT",
    "ADMIN_DEPOSIT",
    "ADMIN_WITHDRAW",
    "ASSET_TRANSFER",
    "AUTO_EXCHANGE",
    "COIN_SWAP_DEPOSIT",
    "COIN_SWAP_WITHDRAW",
    "DEPOSIT",
    "FUNDING_FEE",
    "INSURANCE_CLEAR",
    "MARGIN_TRANSFER",
    "MARGIN_TYPE_CHANGE",
    "OPTIONS_PREMIUM_FEE",
    "OPTIONS_SETTLE_PROFIT",
    "ORDER",
    "WITHDRAW",
    "WITHDRAW_REJECT",
}
_POSITION_SIDES = {"BOTH", "LONG", "SHORT"}
_MARGIN_TYPES = {"cross", "isolated"}
_EVENT_FIELDS = {"e", "E", "T", "a"}
_ACCOUNT_FIELDS = {"m", "B", "P"}
_BALANCE_FIELDS = {"a", "wb", "cw", "bc"}
_POSITION_FIELDS = {"s", "pa", "ep", "bep", "cr", "up", "mt", "iw", "ps", "ma"}


class AccountUpdateError(ValueError):
    """事件缺少必需事实或包含未知结构、字段或枚举。"""


@dataclass(frozen=True)
class ParsedAccountUpdate:
    reason: str
    event_time: datetime
    transaction_time: datetime
    positions: tuple[Position, ...]


def _mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AccountUpdateError(f"invalid object field: {field}")
    return value


def _list(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise AccountUpdateError(f"invalid list field: {field}")
    return value


def _required(data: dict[str, Any], key: str) -> Any:
    value = data.get(key)
    if value is None or value == "":
        raise AccountUpdateError(f"missing account update field: {key}")
    return value


def _reject_unknown(data: dict[str, Any], allowed: set[str], field: str) -> None:
    unknown = set(data) - allowed
    if unknown:
        names = ", ".join(sorted(str(name) for name in unknown))
        raise AccountUpdateError(f"unknown {field} field(s): {names}")


def _decimal(data: dict[str, Any], key: str) -> Decimal:
    try:
        value = Decimal(str(_required(data, key)))
    except (InvalidOperation, ValueError) as exc:
        raise AccountUpdateError(f"invalid decimal field: {key}") from exc
    if not value.is_finite():
        raise AccountUpdateError(f"invalid decimal field: {key}")
    return value


def _datetime_ms(value: Any, field: str) -> datetime:
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OSError) as exc:
        raise AccountUpdateError(f"invalid timestamp field: {field}") from exc


def parse_account_update(
    event: dict[str, Any],
    *,
    account_id: str,
    strategy_id: str,
) -> ParsedAccountUpdate:
    """解析完整 `ACCOUNT_UPDATE` 事件；归属必须由调用方显式提供。"""
    if not account_id or not strategy_id:
        raise AccountUpdateError("account_id and strategy_id are required")
    event = _mapping(event, "event")
    _reject_unknown(event, _EVENT_FIELDS, "event")
    if _required(event, "e") != "ACCOUNT_UPDATE":
        raise AccountUpdateError(f"unknown Binance event type: {event.get('e')}")

    event_time = _datetime_ms(_required(event, "E"), "E")
    transaction_time = _datetime_ms(_required(event, "T"), "T")
    account = _mapping(_required(event, "a"), "a")
    _reject_unknown(account, _ACCOUNT_FIELDS, "account")

    reason = str(_required(account, "m"))
    if reason not in _ACCOUNT_UPDATE_REASONS:
        raise AccountUpdateError(f"unknown Binance account update reason: {reason}")

    balances = _list(_required(account, "B"), "a.B")
    for index, raw_balance in enumerate(balances):
        balance = _mapping(raw_balance, f"a.B[{index}]")
        _reject_unknown(balance, _BALANCE_FIELDS, f"balance {index}")

    positions: list[Position] = []
    seen: set[tuple[str, str]] = set()
    for index, raw_position in enumerate(_list(_required(account, "P"), "a.P")):
        data = _mapping(raw_position, f"a.P[{index}]")
        _reject_unknown(data, _POSITION_FIELDS, f"position {index}")
        symbol = str(_required(data, "s"))
        position_side = str(_required(data, "ps"))
        if position_side not in _POSITION_SIDES:
            raise AccountUpdateError(f"unknown Binance position side: {position_side}")
        margin_type = str(_required(data, "mt"))
        if margin_type not in _MARGIN_TYPES:
            raise AccountUpdateError(f"unknown Binance margin type: {margin_type}")

        key = (symbol, position_side)
        if key in seen:
            raise AccountUpdateError(
                f"duplicate Binance position snapshot: {symbol}/{position_side}"
            )
        seen.add(key)

        entry_price = _decimal(data, "ep")
        isolated_margin = _decimal(data, "iw")
        if entry_price < 0 or isolated_margin < 0:
            raise AccountUpdateError("entry price and isolated margin must be non-negative")
        positions.append(
            Position(
                account_id=account_id,
                strategy_id=strategy_id,
                symbol=symbol,
                position_side=position_side,
                quantity=_decimal(data, "pa"),
                entry_price=entry_price,
                unrealized_pnl=_decimal(data, "up"),
                margin_type=margin_type,
                isolated_margin=isolated_margin,
                exchange_time=transaction_time,
            )
        )

    return ParsedAccountUpdate(
        reason=reason,
        event_time=event_time,
        transaction_time=transaction_time,
        positions=tuple(positions),
    )


class BinanceAccountUpdateLedger:
    """将解析成功的一批 Binance 仓位快照原子写入 PostgreSQL。"""

    def __init__(self, db: LedgerDB, *, account_id: str, strategy_id: str):
        if not account_id or not strategy_id:
            raise ValueError("account_id and strategy_id are required")
        self.db = db
        self.account_id = account_id
        self.strategy_id = strategy_id

    async def handle(self, event: dict[str, Any]) -> list[int]:
        update = parse_account_update(
            event,
            account_id=self.account_id,
            strategy_id=self.strategy_id,
        )
        return await self.db.apply_account_update(update.positions)
