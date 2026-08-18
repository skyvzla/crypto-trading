#!/usr/bin/env python3
"""索引驱动的日线异动扫描。

只读本地 Parquet 历史归档和 PostgreSQL 的 ``exchange_symbols`` 元数据。
每个日线振幅事件保留为一行；同一币种的多次事件不会合并。分钟级区间的
定义是日内先出现的极值为起点、随后相反极值首次出现的分钟为终点。
"""

from __future__ import annotations

import argparse
import csv
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd
import psycopg

from trading_platform.market.archive.index import (
    ARCHIVE_INDEX_FILENAME,
    build_archive_index,
    load_archive_index,
    verify_archive_index_files,
)

DAY_MS = 24 * 60 * 60 * 1000
DEFAULT_FILE_BATCH_SIZE = 256

OUTPUT_FIELDS = [
    "symbol",
    "timeframe",
    "open_time_utc",
    "close_time_utc",
    "open",
    "high",
    "low",
    "close",
    "amplitude_percent",
    "scan_start_after_utc",
    "onboard_at_utc",
    "onboard_age_days",
    "new_listing_within_15_days",
    "onboard_source",
    "oscillation_direction",
    "oscillation_start_utc",
    "oscillation_end_utc",
    "oscillation_duration_minutes",
    "oscillation_start_price",
    "oscillation_end_price",
    "minute_data_status",
    "minute_count",
    "minute_gap_count",
    "low_extreme_tie_count",
    "high_extreme_tie_count",
    "daily_low_first_utc",
    "daily_high_first_utc",
    "minute_extremes_match_daily",
    "partial_listing_day",
    "start_near_day_open_30m",
    "end_near_day_close_30m",
]


def _default_dsn() -> str:
    return (
        f"postgresql://{os.getenv('DB_USER', 'postgres')}:"
        f"{os.getenv('DB_PASSWORD', 'postgres')}@"
        f"{os.getenv('DB_HOST', 'localhost')}:"
        f"{os.getenv('DB_PORT', '5432')}/"
        f"{os.getenv('DB_DATABASE', 'trading_platform')}"
    )


def _iso(value_ms: int | None) -> str:
    if value_ms is None:
        return ""
    return datetime.fromtimestamp(value_ms / 1000, tz=timezone.utc).isoformat()


def _load_onboard_times(dsn: str, symbols: list[str]) -> dict[str, int]:
    with psycopg.connect(dsn, connect_timeout=5) as connection:
        connection.execute("SET TRANSACTION READ ONLY")
        rows = connection.execute(
            "SELECT symbol, onboard_date FROM exchange_symbols "
            "WHERE symbol = ANY(%s)",
            (symbols,),
        ).fetchall()
    return {
        str(symbol).strip().upper(): int(onboard_at.timestamp() * 1000)
        for symbol, onboard_at in rows
        if onboard_at is not None
    }


def _daily_events(
    index_path: Path,
    *,
    threshold_percent: float,
    delay_minutes: int,
    onboard_times: dict[str, int],
    new_listing_days: int,
    file_batch_size: int,
) -> tuple[list[dict[str, Any]], int, int]:
    index = load_archive_index(index_path)
    selected = index[index["timeframe"].eq("1d")].drop_duplicates(
        "relative_path"
    )
    if selected.empty:
        raise RuntimeError("archive index contains no 1d partitions")
    verify_archive_index_files(selected, index_path.parent)

    paths = [str(index_path.parent / path) for path in selected["relative_path"]]
    delay_ms = delay_minutes * 60 * 1000
    new_listing_ms = new_listing_days * DAY_MS
    events: list[dict[str, Any]] = []
    connection = duckdb.connect(":memory:")
    try:
        connection.execute("SET TimeZone = 'UTC'")
        for start in range(0, len(paths), file_batch_size):
            files = paths[start : start + file_batch_size]
            rows = connection.execute(
                """
                SELECT symbol, epoch_ms(open_time), epoch_ms(close_time),
                       open, high, low, close,
                       ((high - low) / open) * 100 AS amplitude_percent
                FROM read_parquet(?, union_by_name=true)
                WHERE timeframe = '1d'
                  AND open > 0
                  AND ((high - low) / open) * 100 > ?
                ORDER BY symbol, open_time
                """,
                [files, threshold_percent],
            ).fetchall()
            for symbol, open_ms, close_ms, open_, high, low, close, amplitude in rows:
                normalized_symbol = str(symbol).strip().upper()
                onboard_ms = onboard_times.get(normalized_symbol)
                age_days = (
                    None
                    if onboard_ms is None
                    # 日线收盘时已上市多久；上市当日的日线开盘早于 onboard
                    # 时间，不能以 open_time 计算，否则会把新币误标为非新币。
                    else (int(close_ms) - onboard_ms) / DAY_MS
                )
                events.append({
                    "symbol": normalized_symbol,
                    "timeframe": "1d",
                    "open_time_utc": _iso(int(open_ms)),
                    "close_time_utc": _iso(int(close_ms)),
                    "open": f"{float(open_):.12g}",
                    "high": f"{float(high):.12g}",
                    "low": f"{float(low):.12g}",
                    "close": f"{float(close):.12g}",
                    "amplitude_percent": f"{float(amplitude):.6f}",
                    "scan_start_after_utc": _iso(int(close_ms) + 1 + delay_ms),
                    "onboard_at_utc": _iso(onboard_ms),
                    "onboard_age_days": (
                        "" if age_days is None else f"{age_days:.6f}"
                    ),
                    "new_listing_within_15_days": str(
                        age_days is not None and 0 <= age_days <= new_listing_days
                    ).lower(),
                    "onboard_source": (
                        "exchange_symbols.onboard_date"
                        if onboard_ms is not None
                        else "missing"
                    ),
                })
    finally:
        connection.close()
    return events, len(selected), len(paths)


def _attach_minute_extremes(
    events: list[dict[str, Any]],
    index_path: Path,
    *,
    file_batch_size: int,
) -> tuple[int, int]:
    if not events:
        return 0, 0
    event_rows = []
    for event_id, event in enumerate(events):
        opened = datetime.fromisoformat(str(event["open_time_utc"]))
        event_rows.append({
            "event_id": event_id,
            "symbol": event["symbol"],
            "day_open_ms": int(opened.timestamp() * 1000),
            "year": opened.year,
            "month": opened.month,
        })
    event_frame = pd.DataFrame(event_rows)
    index = load_archive_index(index_path)
    minute_index = index[index["timeframe"].eq("1m")].copy()
    event_months = event_frame[["symbol", "year", "month"]].drop_duplicates()
    source_parts = minute_index.merge(
        event_months, on=["symbol", "year", "month"]
    ).drop_duplicates("relative_path")
    verify_archive_index_files(source_parts, index_path.parent)
    event_sources = event_frame.merge(
        source_parts[["symbol", "year", "month", "relative_path"]],
        on=["symbol", "year", "month"],
        how="left",
    )

    resolved: dict[int, tuple[float, float, int, int, int, int, int, int]] = {}
    connection = duckdb.connect(":memory:")
    try:
        connection.execute("SET TimeZone = 'UTC'")
        paths = source_parts["relative_path"].tolist()
        for start in range(0, len(paths), file_batch_size):
            batch_paths = paths[start : start + file_batch_size]
            batch_events = event_sources[
                event_sources["relative_path"].isin(batch_paths)
            ][["event_id", "symbol", "day_open_ms"]].drop_duplicates("event_id")
            connection.register("target_events", batch_events)
            try:
                rows = connection.execute(
                    """
                    WITH minute_rows AS (
                        SELECT target.event_id,
                               epoch_ms(candle.open_time) AS open_ms,
                               candle.low,
                               candle.high
                        FROM read_parquet(?, union_by_name=true) AS candle
                        JOIN target_events AS target
                          ON candle.symbol = target.symbol
                         AND epoch_ms(candle.open_time) >= target.day_open_ms
                         AND epoch_ms(candle.open_time) < target.day_open_ms + ?
                        WHERE candle.timeframe = '1m'
                    ), extrema AS (
                        SELECT event_id, min(low) AS day_low, max(high) AS day_high
                        FROM minute_rows
                        GROUP BY event_id
                    ), ordered AS (
                        SELECT minute_rows.*, extrema.day_low, extrema.day_high,
                               lag(open_ms) OVER (
                                   PARTITION BY minute_rows.event_id
                                   ORDER BY open_ms
                               ) AS previous_open_ms
                        FROM minute_rows
                        JOIN extrema USING (event_id)
                    )
                    SELECT event_id, day_low, day_high,
                           min(open_ms) FILTER (WHERE low = day_low) AS low_time_ms,
                           min(open_ms) FILTER (WHERE high = day_high) AS high_time_ms,
                           sum(CASE WHEN low = day_low THEN 1 ELSE 0 END) AS low_ties,
                           sum(CASE WHEN high = day_high THEN 1 ELSE 0 END) AS high_ties,
                           count(*) AS minute_count,
                           sum(CASE
                               WHEN previous_open_ms IS NOT NULL
                                AND open_ms - previous_open_ms > 60000
                               THEN 1 ELSE 0
                           END) AS minute_gaps
                    FROM ordered
                    GROUP BY event_id, day_low, day_high
                    """,
                    [
                        [str(index_path.parent / path) for path in batch_paths],
                        DAY_MS,
                    ],
                ).fetchall()
            finally:
                connection.unregister("target_events")
            for (
                event_id,
                low,
                high,
                low_time,
                high_time,
                low_ties,
                high_ties,
                minute_count,
                minute_gaps,
            ) in rows:
                resolved[int(event_id)] = (
                    float(low),
                    float(high),
                    int(low_time),
                    int(high_time),
                    int(low_ties),
                    int(high_ties),
                    int(minute_count),
                    int(minute_gaps),
                )
    finally:
        connection.close()

    for event_id, event in enumerate(events):
        result = resolved.get(event_id)
        if result is None:
            event.update({
                "oscillation_direction": "",
                "oscillation_start_utc": "",
                "oscillation_end_utc": "",
                "oscillation_duration_minutes": "",
                "oscillation_start_price": "",
                "oscillation_end_price": "",
                "minute_data_status": "missing_1m",
                "minute_count": "",
                "minute_gap_count": "",
                "low_extreme_tie_count": "",
                "high_extreme_tie_count": "",
                "daily_low_first_utc": "",
                "daily_high_first_utc": "",
                "minute_extremes_match_daily": "",
            })
            continue
        (
            low,
            high,
            low_time,
            high_time,
            low_ties,
            high_ties,
            minute_count,
            minute_gaps,
        ) = result
        daily_extremes_match = (
            abs(low - float(event["low"])) <= max(1e-12, abs(low) * 1e-8)
            and abs(high - float(event["high"])) <= max(1e-12, abs(high) * 1e-8)
        )
        common = {
            "minute_count": str(minute_count),
            "minute_gap_count": str(minute_gaps),
            "low_extreme_tie_count": str(low_ties),
            "high_extreme_tie_count": str(high_ties),
            "daily_low_first_utc": _iso(low_time),
            "daily_high_first_utc": _iso(high_time),
            "minute_extremes_match_daily": str(daily_extremes_match).lower(),
            "partial_listing_day": "false",
            "start_near_day_open_30m": "false",
            "end_near_day_close_30m": "false",
        }
        if low_time == high_time:
            event.update({
                **common,
                "oscillation_direction": "ambiguous",
                "oscillation_start_utc": "",
                "oscillation_end_utc": "",
                "oscillation_duration_minutes": "",
                "oscillation_start_price": "",
                "oscillation_end_price": "",
                "minute_data_status": "ambiguous_same_minute",
            })
            continue
        if low_time <= high_time:
            direction = "upward"
            start_time, end_time = low_time, high_time
            start_price, end_price = low, high
        else:
            direction = "downward"
            start_time, end_time = high_time, low_time
            start_price, end_price = high, low
        status = (
            "minute_extreme_mismatch"
            if not daily_extremes_match
            else "incomplete_1m"
            if minute_gaps > 0
            else (
                "partial_listing_day"
                if event["onboard_at_utc"]
                and int(datetime.fromisoformat(event["onboard_at_utc"]).timestamp() * 1000)
                >= int(datetime.fromisoformat(event["open_time_utc"]).timestamp() * 1000)
                and int(datetime.fromisoformat(event["onboard_at_utc"]).timestamp() * 1000)
                < int(datetime.fromisoformat(event["open_time_utc"]).timestamp() * 1000) + DAY_MS
                else "partial_1m"
            )
            if minute_count != 1440
            else "resolved"
        )
        event.update({
            **{
                **common,
                "partial_listing_day": str(status == "partial_listing_day").lower(),
                "start_near_day_open_30m": str(
                    start_time - int(datetime.fromisoformat(event["open_time_utc"]).timestamp() * 1000)
                    <= 30 * 60 * 1000
                ).lower(),
                "end_near_day_close_30m": str(
                    end_time - int(datetime.fromisoformat(event["open_time_utc"]).timestamp() * 1000)
                    >= DAY_MS - 30 * 60 * 1000
                ).lower(),
            },
            "oscillation_direction": direction,
            "oscillation_start_utc": _iso(start_time),
            "oscillation_end_utc": _iso(end_time),
            "oscillation_duration_minutes": str((end_time - start_time) // 60_000),
            "oscillation_start_price": f"{start_price:.12g}",
            "oscillation_end_price": f"{end_price:.12g}",
            "minute_data_status": status,
        })
    return len(source_parts), len(resolved)


def _write_csv(output_path: Path, events: list[dict[str, Any]]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="",
        dir=output_path.parent,
        prefix=f".{output_path.name}.",
        suffix=".tmp",
        delete=False,
    ) as stream:
        temporary_path = Path(stream.name)
        writer = csv.DictWriter(stream, fieldnames=OUTPUT_FIELDS)
        writer.writeheader()
        writer.writerows(events)
    temporary_path.replace(output_path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="按 1d 振幅扫描异动并下钻 1m 极值")
    parser.add_argument(
        "--archive-index",
        type=Path,
        default=Path("data/market/candles") / ARCHIVE_INDEX_FILENAME,
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("reports/daily_amplitude_over_50pct.csv"),
    )
    parser.add_argument("--threshold-percent", type=float, default=50.0)
    parser.add_argument("--delay-minutes", type=int, default=45)
    parser.add_argument("--new-listing-days", type=int, default=15)
    parser.add_argument("--file-batch-size", type=int, default=DEFAULT_FILE_BATCH_SIZE)
    parser.add_argument("--database-dsn", default=_default_dsn())
    parser.add_argument(
        "--rebuild-index",
        action="store_true",
        help="扫描前重建本地归档索引；不修改 K 线数据",
    )
    parser.add_argument("--index-workers", type=int, default=8)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.threshold_percent < 0:
        raise ValueError("threshold-percent must be non-negative")
    if args.delay_minutes < 0:
        raise ValueError("delay-minutes must be non-negative")
    if args.new_listing_days < 0:
        raise ValueError("new-listing-days must be non-negative")
    if args.file_batch_size <= 0 or args.index_workers <= 0:
        raise ValueError("batch size and index workers must be positive")

    index_path = args.archive_index.resolve()
    if args.rebuild_index:
        index_path = build_archive_index(index_path.parent, workers=args.index_workers)
    index = load_archive_index(index_path)
    symbols = sorted(index.loc[index["timeframe"].eq("1d"), "symbol"].unique())
    onboard_times = _load_onboard_times(args.database_dsn, symbols)
    events, daily_partitions, _ = _daily_events(
        index_path,
        threshold_percent=args.threshold_percent,
        delay_minutes=args.delay_minutes,
        onboard_times=onboard_times,
        new_listing_days=args.new_listing_days,
        file_batch_size=args.file_batch_size,
    )
    minute_partitions, resolved = _attach_minute_extremes(
        events, index_path, file_batch_size=args.file_batch_size
    )
    events.sort(key=lambda row: (str(row["open_time_utc"]), str(row["symbol"])))
    _write_csv(args.output.resolve(), events)
    print(
        f"daily_partitions={daily_partitions} events={len(events)} "
        f"minute_partitions={minute_partitions} resolved_minutes={resolved} "
        f"missing_minutes={len(events) - resolved} output={args.output.resolve()}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
