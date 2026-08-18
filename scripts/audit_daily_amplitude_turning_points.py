#!/usr/bin/env python3
"""从原始 1d/1m Parquet 独立复核异动报告的行情起止点。"""

from __future__ import annotations

import argparse
import csv
import math
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pandas as pd

from trading_platform.market.archive.index import (
    ARCHIVE_INDEX_FILENAME,
    load_archive_index,
    verify_archive_index_files,
)

DAY_MS = 86_400_000
FILE_BATCH_SIZE = 128


def _iso(value_ms: int) -> str:
    return datetime.fromtimestamp(value_ms / 1000, tz=timezone.utc).isoformat()


def _matches(left: float, right: float) -> bool:
    return math.isclose(left, right, rel_tol=1e-8, abs_tol=1e-12)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="独立复核日线异动的分钟级起止点")
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("reports/daily_amplitude_over_50pct.csv"),
    )
    parser.add_argument(
        "--archive-index",
        type=Path,
        default=Path("data/market/candles") / ARCHIVE_INDEX_FILENAME,
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("reports/daily_amplitude_turning_point_audit.csv"),
    )
    return parser.parse_args()


def _source_parts(
    index: pd.DataFrame, events: pd.DataFrame, timeframe: str
) -> tuple[pd.DataFrame, pd.DataFrame]:
    source_index = index[index["timeframe"].eq(timeframe)]
    event_months = events[["symbol", "year", "month"]].drop_duplicates()
    parts = source_index.merge(
        event_months, on=["symbol", "year", "month"]
    ).drop_duplicates("relative_path")
    mapping = events.merge(
        parts[["symbol", "year", "month", "relative_path"]],
        on=["symbol", "year", "month"],
        how="left",
    )
    return parts, mapping


def _daily_source(
    connection: duckdb.DuckDBPyConnection,
    root: Path,
    parts: pd.DataFrame,
    mapping: pd.DataFrame,
) -> dict[int, tuple[float, float, float, float]]:
    values: dict[int, tuple[float, float, float, float]] = {}
    paths = parts["relative_path"].tolist()
    for start in range(0, len(paths), FILE_BATCH_SIZE):
        batch = paths[start : start + FILE_BATCH_SIZE]
        events = mapping[mapping["relative_path"].isin(batch)][
            ["event_id", "symbol", "day_open_ms"]
        ].drop_duplicates("event_id")
        connection.register("target_events", events)
        try:
            rows = connection.execute(
                """
                SELECT target.event_id, candle.open, candle.high, candle.low, candle.close
                FROM read_parquet(?, union_by_name=true) AS candle
                JOIN target_events AS target
                  ON candle.symbol = target.symbol
                 AND epoch_ms(candle.open_time) = target.day_open_ms
                WHERE candle.timeframe = '1d'
                """,
                [[str(root / path) for path in batch]],
            ).fetchall()
        finally:
            connection.unregister("target_events")
        values.update({
            int(event_id): (float(open_), float(high), float(low), float(close))
            for event_id, open_, high, low, close in rows
        })
    return values


def _minute_source(
    connection: duckdb.DuckDBPyConnection,
    root: Path,
    parts: pd.DataFrame,
    mapping: pd.DataFrame,
) -> dict[int, tuple[float, float, int, int, int, int, int, int]]:
    values: dict[int, tuple[float, float, int, int, int, int, int, int]] = {}
    paths = parts["relative_path"].tolist()
    for start in range(0, len(paths), FILE_BATCH_SIZE):
        batch = paths[start : start + FILE_BATCH_SIZE]
        events = mapping[mapping["relative_path"].isin(batch)][
            ["event_id", "symbol", "day_open_ms"]
        ].drop_duplicates("event_id")
        connection.register("target_events", events)
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
                    FROM minute_rows GROUP BY event_id
                ), ordered AS (
                    SELECT minute_rows.*, extrema.day_low, extrema.day_high,
                           lag(open_ms) OVER (
                               PARTITION BY minute_rows.event_id ORDER BY open_ms
                           ) AS previous_open_ms
                    FROM minute_rows JOIN extrema USING (event_id)
                )
                SELECT event_id, day_low, day_high,
                       min(open_ms) FILTER (WHERE low = day_low) AS low_first_ms,
                       min(open_ms) FILTER (WHERE high = day_high) AS high_first_ms,
                       sum(CASE WHEN low = day_low THEN 1 ELSE 0 END) AS low_ties,
                       sum(CASE WHEN high = day_high THEN 1 ELSE 0 END) AS high_ties,
                       count(*) AS minute_count,
                       sum(CASE WHEN previous_open_ms IS NOT NULL
                                 AND open_ms - previous_open_ms > 60000
                                THEN 1 ELSE 0 END) AS minute_gaps
                FROM ordered
                GROUP BY event_id, day_low, day_high
                """,
                [[str(root / path) for path in batch], DAY_MS],
            ).fetchall()
        finally:
            connection.unregister("target_events")
        values.update({
            int(event_id): (
                float(low), float(high), int(low_first), int(high_first),
                int(low_ties), int(high_ties), int(minute_count), int(gaps),
            )
            for (
                event_id, low, high, low_first, high_first, low_ties,
                high_ties, minute_count, gaps,
            ) in rows
        })
    return values


def main() -> int:
    args = parse_args()
    with args.report.open(encoding="utf-8", newline="") as stream:
        report_rows = list(csv.DictReader(stream))
    if not report_rows:
        raise ValueError("report contains no rows")

    records = []
    for event_id, row in enumerate(report_rows):
        opened = datetime.fromisoformat(row["open_time_utc"])
        records.append({
            "event_id": event_id,
            "symbol": row["symbol"],
            "day_open_ms": int(opened.timestamp() * 1000),
            "year": opened.year,
            "month": opened.month,
        })
    events = pd.DataFrame(records)
    index_path = args.archive_index.resolve()
    index = load_archive_index(index_path)
    daily_parts, daily_mapping = _source_parts(index, events, "1d")
    minute_parts, minute_mapping = _source_parts(index, events, "1m")
    verify_archive_index_files(daily_parts, index_path.parent)
    verify_archive_index_files(minute_parts, index_path.parent)

    connection = duckdb.connect(":memory:")
    try:
        connection.execute("SET TimeZone = 'UTC'")
        daily = _daily_source(connection, index_path.parent, daily_parts, daily_mapping)
        minute = _minute_source(connection, index_path.parent, minute_parts, minute_mapping)
    finally:
        connection.close()

    audit_rows = []
    failures = 0
    for event_id, report in enumerate(report_rows):
        source_daily = daily.get(event_id)
        source_minute = minute.get(event_id)
        daily_match = source_daily is not None and all(
            _matches(source, float(report[column]))
            for source, column in zip(source_daily, ("open", "high", "low", "close"))
        )
        values: dict[str, object] = {
            "symbol": report["symbol"],
            "open_time_utc": report["open_time_utc"],
            "daily_ohlc_matches_source": str(daily_match).lower(),
            "report_minute_status": report["minute_data_status"],
        }
        if source_minute is None:
            values.update({
                "source_minute_status": "missing_1m",
                "minute_extremes_match_daily": "",
                "first_extremes_match_report": "",
                "low_first_utc": "",
                "high_first_utc": "",
                "low_extreme_tie_count": "",
                "high_extreme_tie_count": "",
                "minute_count": "",
                "minute_gap_count": "",
                "start_near_day_open_30m": "",
                "end_near_day_close_30m": "",
            })
            consistent = report["minute_data_status"] == "missing_1m" and daily_match
        else:
            low, high, low_first, high_first, low_ties, high_ties, count, gaps = source_minute
            extremes_match = _matches(low, float(report["low"])) and _matches(
                high, float(report["high"])
            )
            same_minute = low_first == high_first
            if same_minute:
                source_status = "ambiguous_same_minute"
                report_match = report["minute_data_status"] == source_status
            else:
                direction = "upward" if low_first < high_first else "downward"
                start_ms, end_ms = (
                    (low_first, high_first)
                    if direction == "upward"
                    else (high_first, low_first)
                )
                source_status = "incomplete_1m" if gaps else "partial_1m" if count != 1440 else "resolved"
                report_match = (
                    report["oscillation_direction"] == direction
                    and report["oscillation_start_utc"] == _iso(start_ms)
                    and report["oscillation_end_utc"] == _iso(end_ms)
                    and report["minute_data_status"]
                    in {source_status, "partial_listing_day"}
                )
            values.update({
                "source_minute_status": source_status,
                "minute_extremes_match_daily": str(extremes_match).lower(),
                "first_extremes_match_report": str(report_match).lower(),
                "low_first_utc": _iso(low_first),
                "high_first_utc": _iso(high_first),
                "low_extreme_tie_count": low_ties,
                "high_extreme_tie_count": high_ties,
                "minute_count": count,
                "minute_gap_count": gaps,
                "start_near_day_open_30m": str(
                    min(low_first, high_first) - events.iloc[event_id].day_open_ms <= 1_800_000
                ).lower(),
                "end_near_day_close_30m": str(
                    max(low_first, high_first) - events.iloc[event_id].day_open_ms >= DAY_MS - 1_800_000
                ).lower(),
            })
            consistent = daily_match and extremes_match and report_match
        values["audit_status"] = "pass" if consistent else "report_mismatch"
        if not consistent:
            failures += 1
        audit_rows.append(values)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", newline="", dir=args.output.parent,
        prefix=f".{args.output.name}.", suffix=".tmp", delete=False,
    ) as stream:
        temporary = Path(stream.name)
        writer = csv.DictWriter(stream, fieldnames=list(audit_rows[0]))
        writer.writeheader()
        writer.writerows(audit_rows)
    temporary.replace(args.output)
    print(
        f"events={len(audit_rows)} failures={failures} output={args.output.resolve()}"
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
