"""Strictly aligned public 5-minute derivatives metrics."""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from typing import Any, Literal


METRICS_INTERVAL_MS = 5 * 60 * 1000
METRICS_MAX_AGE_MS = 2 * METRICS_INTERVAL_MS
METRICS_STREAM_MAXLEN = 576


class MetricsValidationError(ValueError):
    pass


@dataclass(frozen=True)
class MarketMetrics5m:
    symbol: str
    available_time: int
    open_interest: float
    long_short_ratio: float

    def to_dict(self) -> dict[str, str | int | float]:
        return {
            "symbol": self.symbol,
            "available_time": self.available_time,
            "open_interest": self.open_interest,
            "long_short_ratio": self.long_short_ratio,
        }


def align_metrics_rows(
    symbol: str,
    open_interest_rows: Any,
    long_short_rows: Any,
    *,
    now_ms: int | None = None,
) -> MarketMetrics5m:
    """Select the newest common, completed 5m bucket from both endpoints."""

    normalized_symbol = symbol.strip().upper()
    if not normalized_symbol:
        raise MetricsValidationError("metrics symbol is required")
    current = int(time.time() * 1000) if now_ms is None else now_ms
    open_interest = _rows_by_time(
        open_interest_rows,
        symbol=normalized_symbol,
        value_field="sumOpenInterest",
    )
    long_short = _rows_by_time(
        long_short_rows,
        symbol=normalized_symbol,
        value_field="longShortRatio",
    )
    common = sorted(set(open_interest) & set(long_short), reverse=True)
    completed = [timestamp for timestamp in common if timestamp + METRICS_INTERVAL_MS <= current]
    if not completed:
        raise MetricsValidationError("metrics endpoints have no common completed 5m bucket")
    timestamp = completed[0]
    available_time = timestamp + METRICS_INTERVAL_MS
    if current - available_time > METRICS_MAX_AGE_MS:
        raise MetricsValidationError("latest common metrics bucket is stale")
    return MarketMetrics5m(
        symbol=normalized_symbol,
        available_time=available_time,
        open_interest=open_interest[timestamp],
        long_short_ratio=long_short[timestamp],
    )


def _rows_by_time(
    rows: Any, *, symbol: str, value_field: str
) -> dict[int, float]:
    if not isinstance(rows, list) or not rows:
        raise MetricsValidationError("metrics response must be a non-empty list")
    result: dict[int, float] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise MetricsValidationError("metrics row must be an object")
        row_symbol = str(row.get("symbol", symbol)).strip().upper()
        if row_symbol != symbol:
            raise MetricsValidationError("metrics response contains an unexpected symbol")
        try:
            timestamp = int(row["timestamp"])
            value = float(row[value_field])
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise MetricsValidationError("metrics row has invalid fields") from exc
        if timestamp < 0 or timestamp % METRICS_INTERVAL_MS != 0:
            raise MetricsValidationError("metrics timestamp is not aligned to 5m")
        if not math.isfinite(value) or value < 0:
            raise MetricsValidationError("metrics value must be finite and non-negative")
        previous = result.get(timestamp)
        if previous is not None and previous != value:
            raise MetricsValidationError("metrics response has conflicting duplicate rows")
        result[timestamp] = value
    return result


class MetricsRedisStore:
    _PUBLISH_SCRIPT = """
local watermark_raw = redis.call('HGET', KEYS[1], 'watermark')
local latest_raw = redis.call('HGET', KEYS[1], 'latest')
local watermark = nil
local latest = nil

if watermark_raw then
    watermark = tonumber(watermark_raw)
    if not watermark or watermark < 0 or watermark % 1 ~= 0 then return -2 end
end

if latest_raw then
    local decoded, value = pcall(cjson.decode, latest_raw)
    if not decoded or type(value) ~= 'table' then return -2 end
    latest = value
    local latest_time = tonumber(value.available_time)
    if not latest_time or latest_time < 0 or latest_time % 1 ~= 0 then return -2 end
    if watermark and watermark ~= latest_time then return -2 end
    if not watermark then
        watermark = latest_time
        redis.call('HSET', KEYS[1], 'watermark', string.format('%.0f', watermark))
    end
elseif watermark then
    return -2
end

local incoming_time = tonumber(ARGV[1])
if not incoming_time or incoming_time < 0 or incoming_time % 1 ~= 0 then return -2 end
if watermark then
    if watermark > incoming_time then return 0 end
    if watermark == incoming_time then
        local incoming = cjson.decode(ARGV[2])
        if latest.symbol == incoming.symbol
            and tonumber(latest.available_time) == tonumber(incoming.available_time)
            and tonumber(latest.open_interest) == tonumber(incoming.open_interest)
            and tonumber(latest.long_short_ratio) == tonumber(incoming.long_short_ratio) then
            return 0
        end
        return -1
    end
end

redis.call('HSET', KEYS[1],
    'watermark', ARGV[1],
    'latest', ARGV[2])
redis.call('XADD', KEYS[2],
    'MAXLEN', '~', ARGV[3],
    '*', 'data', ARGV[2])
return 1
"""

    def __init__(self, redis_client: Any) -> None:
        self.redis = redis_client

    async def publish(self, event: MarketMetrics5m) -> bool:
        key = f"metrics:{event.symbol}:5m"
        stream = f"metrics:stream:{event.symbol}:5m"
        message = json.dumps(event.to_dict(), separators=(",", ":"), sort_keys=True)
        result = int(
            await self.redis.eval(
                self._PUBLISH_SCRIPT,
                2,
                key,
                stream,
                str(event.available_time),
                message,
                str(METRICS_STREAM_MAXLEN),
            )
        )
        if result == 1:
            return True
        if result == 0:
            return False
        if result == -1:
            raise MetricsValidationError("conflicting duplicate metrics event")
        raise MetricsValidationError("stored metrics watermark is invalid")


QualityStatus = Literal["awaiting_data", "healthy", "degraded"]


@dataclass
class MetricsQuality:
    symbol: str
    status: QualityStatus = "awaiting_data"
    last_available_time: int | None = None
    last_received_at_ms: int | None = None
    issue: str | None = None

    def to_dict(self, *, now_ms: int) -> dict[str, Any]:
        status = self.status
        issue = self.issue
        if (
            status == "healthy"
            and self.last_available_time is not None
            and now_ms - self.last_available_time > METRICS_MAX_AGE_MS
        ):
            status = "degraded"
            issue = "metrics_stale"
        return {
            "symbol": self.symbol,
            "status": status,
            "last_available_time": self.last_available_time,
            "last_received_at_ms": self.last_received_at_ms,
            "issue": issue,
        }


class MetricsQualityTracker:
    def __init__(self) -> None:
        self._symbols: dict[str, MetricsQuality] = {}

    def set_expected_symbols(self, symbols: set[str]) -> None:
        expected = {symbol.strip().upper() for symbol in symbols}
        self._symbols = {
            symbol: self._symbols.get(symbol, MetricsQuality(symbol=symbol))
            for symbol in expected
        }

    def mark_healthy(self, event: MarketMetrics5m, *, received_at_ms: int) -> None:
        quality = self._symbols.get(event.symbol)
        if quality is None:
            return
        quality.status = "healthy"
        quality.last_available_time = event.available_time
        quality.last_received_at_ms = received_at_ms
        quality.issue = None

    def mark_failed(self, symbol: str, issue: str) -> None:
        quality = self._symbols.get(symbol)
        if quality is None:
            return
        quality.status = "degraded"
        quality.issue = issue

    def snapshot(self, *, now_ms: int | None = None) -> dict[str, dict[str, Any]]:
        current = int(time.time() * 1000) if now_ms is None else now_ms
        return {
            symbol: self._symbols[symbol].to_dict(now_ms=current)
            for symbol in sorted(self._symbols)
        }

    @property
    def ready(self) -> bool:
        return all(item["status"] == "healthy" for item in self.snapshot().values())

    @property
    def issue_count(self) -> int:
        return sum(item["status"] != "healthy" for item in self.snapshot().values())
