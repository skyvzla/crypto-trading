"""Deterministic market-data backfill helpers.

The module deliberately contains no I/O.  Callers fetch Binance REST pages and
pass the pages here; this keeps recovery decisions easy to test and retry.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable

from trading_platform.shared.events import Kline


class RecoveryError(ValueError):
    """Input cannot produce a trustworthy, continuous backfill."""


def _interval_ms(interval: str) -> int:
    units = {"m": 60_000, "h": 3_600_000, "d": 86_400_000}
    if not isinstance(interval, str) or len(interval) < 2 or interval[-1] not in units:
        raise RecoveryError(f"unsupported interval: {interval!r}")
    try:
        value = int(interval[:-1])
    except (TypeError, ValueError) as exc:
        raise RecoveryError(f"invalid interval: {interval!r}") from exc
    if value <= 0:
        raise RecoveryError(f"invalid interval: {interval!r}")
    return value * units[interval[-1]]


def normalize_aggtrade(row: dict[str, Any]) -> dict[str, Any]:
    """Convert one Binance aggTrade row to the WebSocket trade shape."""
    if not isinstance(row, dict):
        raise RecoveryError("aggTrade row must be an object")
    required = ("a", "p", "q", "f", "l", "T", "m")
    if any(key not in row for key in required):
        raise RecoveryError("aggTrade row is missing a, p, q, or T")
    try:
        return {
            "agg_trade_id": int(row["a"]),
            "price": Decimal(str(row["p"])),
            "quantity": Decimal(str(row["q"])),
            "first_trade_id": int(row["f"]),
            "last_trade_id": int(row["l"]),
            "timestamp": int(row["T"]),
            "is_buyer_maker": bool(row["m"]),
        }
    except (TypeError, ValueError, ArithmeticError) as exc:
        raise RecoveryError("invalid aggTrade values") from exc


def recover_aggtrades(
    rows: Iterable[dict[str, Any]],
    *,
    expected_start_id: int | None = None,
    expected_end_id: int | None = None,
    max_items: int = 1000,
) -> list[dict[str, Any]]:
    """Normalize, deduplicate and validate a batch of aggTrades."""
    if not 1 <= max_items <= 1000:
        raise RecoveryError("max_items must be between 1 and 1000")
    unique: dict[int, dict[str, Any]] = {}
    for row in rows:
        item = normalize_aggtrade(row)
        previous = unique.get(item["agg_trade_id"])
        if previous is not None and previous != item:
            raise RecoveryError("conflicting duplicate aggTrade")
        unique[item["agg_trade_id"]] = item
    if len(unique) > max_items:
        raise RecoveryError(f"aggTrade backfill exceeds limit {max_items}")
    result = [unique[key] for key in sorted(unique)]
    if result:
        if expected_start_id is not None and result[0]["agg_trade_id"] != expected_start_id:
            raise RecoveryError("aggTrade backfill starts with a gap")
        if expected_end_id is not None and result[-1]["agg_trade_id"] != expected_end_id:
            raise RecoveryError("aggTrade backfill ends with a gap")
        for previous, current in zip(result, result[1:]):
            if current["agg_trade_id"] != previous["agg_trade_id"] + 1:
                raise RecoveryError("aggTrade backfill contains a gap")
    elif expected_start_id is not None and expected_end_id is not None and expected_start_id <= expected_end_id:
        raise RecoveryError("empty aggTrade backfill")
    return result


def normalize_kline(row: list[Any] | tuple[Any, ...], symbol: str, interval: str) -> Kline:
    """Convert one Binance REST kline array to a completed ``Kline``."""
    _interval_ms(interval)
    if not isinstance(row, (list, tuple)) or len(row) < 7:
        raise RecoveryError("kline row must contain at least 7 fields")
    try:
        open_time, open_, high, low, close, volume, close_time = row[:7]
        step = _interval_ms(interval)
        if int(close_time) != int(open_time) + step - 1:
            raise RecoveryError("kline close time is inconsistent with interval")
        return Kline(symbol=symbol, interval=interval, open_time=int(open_time),
                     close_time=int(close_time), available_time=int(close_time) + 1,
                     open=Decimal(str(open_)), high=Decimal(str(high)),
                     low=Decimal(str(low)), close=Decimal(str(close)),
                     volume=Decimal(str(volume)))
    except (TypeError, ValueError, ArithmeticError) as exc:
        raise RecoveryError("invalid kline values") from exc


def recover_klines(
    rows: Iterable[list[Any] | tuple[Any, ...]],
    symbol: str,
    interval: str,
    *,
    max_items: int = 1500,
    now_ms: int | None = None,
) -> list[Kline]:
    """Normalize, drop duplicate candles, and require contiguous open times."""
    if not symbol:
        raise RecoveryError("symbol is required")
    if not 1 <= max_items <= 1500:
        raise RecoveryError("max_items must be between 1 and 1500")
    step = _interval_ms(interval)
    unique: dict[int, Kline] = {}
    for row in rows:
        candle = normalize_kline(row, symbol, interval)
        if now_ms is not None and candle.close_time >= now_ms:
            continue
        previous = unique.get(candle.open_time)
        if previous is not None and previous != candle:
            raise RecoveryError("conflicting duplicate kline")
        unique[candle.open_time] = candle
    if len(unique) > max_items:
        raise RecoveryError(f"kline backfill exceeds limit {max_items}")
    result = [unique[key] for key in sorted(unique)]
    for previous, current in zip(result, result[1:]):
        if current.open_time != previous.open_time + step:
            raise RecoveryError("kline backfill contains a gap")
    return result


@dataclass(frozen=True)
class RecoveryCoordinator:
    """Small facade useful to inject limits consistently at call sites."""

    aggtrade_limit: int = 1000
    kline_limit: int = 1500

    @staticmethod
    def interval_ms(interval: str) -> int:
        return _interval_ms(interval)

    def aggtrades(self, rows: Iterable[dict[str, Any]], **kwargs: Any) -> list[dict[str, Any]]:
        return recover_aggtrades(rows, max_items=self.aggtrade_limit, **kwargs)

    def klines(self, rows: Iterable[list[Any] | tuple[Any, ...]], symbol: str, interval: str, **kwargs: Any) -> list[Kline]:
        return recover_klines(rows, symbol, interval, max_items=self.kline_limit, **kwargs)
