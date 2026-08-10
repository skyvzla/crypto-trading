"""Chart-ready Binance and immutable archive candle readers."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import duckdb
import httpx

from trading_platform.market.archive.index import load_archive_index


BINANCE_KLINE_URL = "https://fapi.binance.com/fapi/v1/klines"
INTERVAL_MS = {
    "1s": 1_000,
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "6h": 21_600_000,
    "8h": 28_800_000,
    "12h": 43_200_000,
    "1d": 86_400_000,
}
_DUCKDB_INTERVAL = {
    "1s": "1 second",
    "1m": "1 minute",
    "5m": "5 minutes",
    "15m": "15 minutes",
    "1h": "1 hour",
    "4h": "4 hours",
    "6h": "6 hours",
    "8h": "8 hours",
    "12h": "12 hours",
    "1d": "1 day",
}
_SYMBOL = re.compile(r"^[A-Z0-9]{2,32}$")
MAX_CANDLES = 5_000


def validate_candle_request(
    symbol: str, interval: str, start_ms: int, end_ms: int
) -> tuple[str, str]:
    normalized_symbol = symbol.strip().upper()
    normalized_interval = interval.strip().lower()
    if not _SYMBOL.fullmatch(normalized_symbol):
        raise ValueError("invalid Binance symbol")
    if normalized_interval not in INTERVAL_MS:
        raise ValueError("unsupported candle interval")
    if start_ms < 0 or start_ms >= end_ms:
        raise ValueError("start_ms must be earlier than end_ms")
    interval_ms = INTERVAL_MS[normalized_interval]
    first_open = ((start_ms + interval_ms - 1) // interval_ms) * interval_ms
    last_open = ((end_ms - 1) // interval_ms) * interval_ms
    estimated = max(0, (last_open - first_open) // interval_ms + 1)
    if estimated > MAX_CANDLES:
        raise ValueError(f"candle range exceeds {MAX_CANDLES} bars")
    return normalized_symbol, normalized_interval


async def fetch_binance_candles(
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    *,
    client: httpx.AsyncClient | None = None,
) -> list[dict[str, Any]]:
    symbol, interval = validate_candle_request(symbol, interval, start_ms, end_ms)
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=15.0)
    rows: list[list[Any]] = []
    cursor = start_ms
    try:
        while cursor < end_ms and len(rows) < MAX_CANDLES:
            response = await client.get(
                BINANCE_KLINE_URL,
                params={
                    "symbol": symbol,
                    "interval": interval,
                    "startTime": cursor,
                    "endTime": end_ms - 1,
                    "limit": min(1500, MAX_CANDLES - len(rows)),
                },
            )
            response.raise_for_status()
            batch = response.json()
            if not isinstance(batch, list):
                raise RuntimeError("Binance returned an invalid Kline payload")
            if not batch:
                break
            rows.extend(batch)
            next_cursor = int(batch[-1][0]) + INTERVAL_MS[interval]
            if next_cursor <= cursor:
                break
            cursor = next_cursor
            if len(batch) < 1500:
                break
    finally:
        if owns_client:
            await client.aclose()
    return [
        {
            "time": int(row[0]) // 1000,
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
            "volume": float(row[5]),
        }
        for row in rows
        if int(row[0]) < end_ms
    ]


def load_archive_candles(
    archive_index_path: str | Path,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
) -> list[dict[str, Any]]:
    symbol, interval = validate_candle_request(symbol, interval, start_ms, end_ms)
    index_path = Path(archive_index_path).resolve()
    if not index_path.is_file():
        raise FileNotFoundError(f"archive index not found: {index_path}")
    index = load_archive_index(index_path)
    preferred = interval if interval in {"1s", "1m", "5m", "15m"} else "1m"
    selected = index[
        (index["symbol"] == symbol)
        & (index["timeframe"] == preferred)
        & (index["first_open_ms"] < end_ms)
        & (index["last_close_ms"] >= start_ms)
    ]
    if selected.empty and preferred != "1m":
        preferred = "1m"
        selected = index[
            (index["symbol"] == symbol)
            & (index["timeframe"] == preferred)
            & (index["first_open_ms"] < end_ms)
            & (index["last_close_ms"] >= start_ms)
        ]
    if selected.empty:
        raise ValueError(f"archive has no {symbol} candles for requested range")
    files = [str(index_path.parent / value) for value in selected["relative_path"]]
    target = _DUCKDB_INTERVAL[interval]
    connection = duckdb.connect(":memory:")
    try:
        connection.execute("SET TimeZone = 'UTC'")
        if preferred == interval:
            query = """
                SELECT epoch_ms(open_time), open, high, low, close, volume
                FROM read_parquet(?, union_by_name=true)
                WHERE symbol = ? AND timeframe = ?
                  AND epoch_ms(open_time) >= ? AND epoch_ms(open_time) < ?
                ORDER BY open_time
            """
            rows = connection.execute(
                query, (files, symbol, preferred, start_ms, end_ms)
            ).fetchall()
        else:
            source_start_ms = (start_ms // INTERVAL_MS[interval]) * INTERVAL_MS[interval]
            first_full_bucket_ms = (
                (start_ms + INTERVAL_MS[interval] - 1) // INTERVAL_MS[interval]
            ) * INTERVAL_MS[interval]
            last_full_bucket_ms = (
                (end_ms - 1) // INTERVAL_MS[interval]
            ) * INTERVAL_MS[interval]
            query = f"""
                WITH source AS (
                    SELECT *, time_bucket(INTERVAL '{target}', open_time) AS bucket
                    FROM read_parquet(?, union_by_name=true)
                      WHERE symbol = ? AND timeframe = ?
                  AND epoch_ms(open_time) >= ? AND epoch_ms(open_time) < ?
                ), aggregated AS (
                    SELECT epoch_ms(bucket) AS bucket_ms,
                           first(open ORDER BY open_time) AS open,
                           max(high) AS high,
                           min(low) AS low,
                           first(close ORDER BY open_time DESC) AS close,
                           sum(volume) AS volume
                    FROM source GROUP BY bucket
                )
                SELECT bucket_ms, open, high, low, close, volume
                FROM aggregated
                WHERE bucket_ms >= ? AND bucket_ms <= ?
                ORDER BY bucket_ms
            """
            rows = connection.execute(
                query,
                (
                    files,
                    symbol,
                    preferred,
                    source_start_ms,
                    end_ms,
                    first_full_bucket_ms,
                    last_full_bucket_ms,
                ),
            ).fetchall()
    finally:
        connection.close()
    return [
        {
            "time": int(row[0]) // 1000,
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
            "volume": float(row[5]),
        }
        for row in rows
    ]
