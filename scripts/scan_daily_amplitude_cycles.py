#!/usr/bin/env python3
"""基于 1d 异动初筛事件，用 1m 数据重新确定完整行情周期。

流程：读取日线振幅异动初筛事件（CSV 报告或直接扫描 1d 归档），对每个
异动日的 1m 数据做 ZigZag 摆动分割 + 短波段合并，输出完整行情周期
（起止时间/方向/时长/首尾价/周期内极值）。

解决原"日内极值两点法"的两类失真：
- 插针日（如 AKEUSDT 2026-07-27）：日高/日低由单根插针产生，原方法
  只标记 3 分钟，丢失完整行情段；
- 中途大回撤日（如 AKEUSDT 2026-07-18）：原方法把 up-down-up 三段
  误标为连续上涨。
"""

from __future__ import annotations

import argparse
import csv
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scan_daily_amplitude_waves import (  # noqa: E402
    _build_waves,
    _contains_spike,
    _zigzag_pivots,
)

from trading_platform.market.archive.index import (  # noqa: E402
    ARCHIVE_INDEX_FILENAME,
    load_archive_index,
    verify_archive_index_files,
)

DAY_MS = 86_400_000

OUTPUT_FIELDS = [
    "symbol",
    "event_day_utc",
    "event_direction",
    "event_amplitude_percent",
    "cycle_index",
    "cycle_count",
    "category",
    "direction",
    "start_utc",
    "end_utc",
    "duration_minutes",
    "start_price",
    "end_price",
    "amplitude_percent",
    "range_percent",
    "high_price",
    "high_utc",
    "low_price",
    "low_utc",
    "contains_spike",
    "spike_direction",
    "merged",
    "covers_day_extreme",
]

CATEGORY_NAMES = {
    "spike_up": "插针涨",
    "spike_down": "插针跌",
    "up_trend": "缓涨",
    "down_trend": "缓跌",
    "quiet": "平静",
}


def _classify_cycle(
    direction: str, amplitude_percent: str, spike_direction: str, threshold: float
) -> str:
    """按形态分类：插针涨/插针跌/缓涨/缓跌/平静。

    - 有插针（up/down/both）：按整体方向归入插针涨/插针跌；
    - 无插针且累计涨跌幅达到阈值：缓涨/缓跌（连续中小 bar 累积）；
    - 其余：平静。
    """
    if spike_direction != "none":
        return "spike_up" if direction == "up" else "spike_down"
    amp = float(amplitude_percent)
    if direction == "up" and amp >= threshold:
        return "up_trend"
    if direction == "down" and amp <= -threshold:
        return "down_trend"
    return "quiet"


def _iso(value_ms: int) -> str:
    return datetime.fromtimestamp(value_ms / 1000, tz=timezone.utc).isoformat()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="1d 异动初筛 + 1m 完整行情周期确定"
    )
    parser.add_argument(
        "--report",
        type=Path,
        help="初筛 CSV（含 symbol/event_1d_open_time_utc）；缺省则直接扫描 1d",
    )
    parser.add_argument(
        "--archive-index",
        type=Path,
        default=Path("data/market/candles") / ARCHIVE_INDEX_FILENAME,
    )
    parser.add_argument(
        "--threshold-percent",
        type=float,
        default=50.0,
        help="直接扫描 1d 时的振幅阈值百分比，默认 50",
    )
    parser.add_argument(
        "--zigzag-percent",
        type=float,
        default=30.0,
        help="摆动确认阈值百分比，默认 30",
    )
    parser.add_argument(
        "--spike-threshold",
        type=float,
        default=0.15,
        help="单根 1m bar 的 (high-low)/low 超过该比例视为插针，默认 0.15",
    )
    parser.add_argument(
        "--merge-short-minutes",
        type=int,
        default=30,
        help="时长小于该值的短波段合并进相邻主波段，默认 30；0 关闭",
    )
    parser.add_argument(
        "--spike-filter",
        choices=["up", "down", "both", "none"],
        default=None,
        help="只输出插针方向为该值的周期（up=上涨插针，down=暴跌插针）",
    )
    parser.add_argument(
        "--classify-threshold",
        type=float,
        default=30.0,
        help="形态分类中缓涨/缓跌的累计涨跌幅阈值，默认 30；0 关闭分类",
    )
    parser.add_argument(
        "--split-categories",
        action="store_true",
        help="按形态分类输出 5 个文件（spike_up/spike_down/up_trend/down_trend/quiet）",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("reports/daily_amplitude_cycles.csv"),
    )
    parser.add_argument(
        "--file-batch-size",
        type=int,
        default=256,
    )
    return parser.parse_args()


def _report_events(report: Path) -> list[dict[str, object]]:
    with report.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    events = []
    for row in rows:
        opened = datetime.fromisoformat(row["event_1d_open_time_utc"])
        events.append({
            "symbol": row["symbol"].strip().upper(),
            "day_ms": int(opened.timestamp() * 1000),
            "event_direction": row.get("direction", ""),
            "event_amplitude_percent": row.get("amplitude_percent", ""),
        })
    return events


def _scan_1d_events(
    index_path: Path, threshold_percent: float, file_batch_size: int
) -> list[dict[str, object]]:
    index = load_archive_index(index_path)
    selected = index[index["timeframe"].eq("1d")].drop_duplicates("relative_path")
    if selected.empty:
        raise RuntimeError("archive index contains no 1d partitions")
    verify_archive_index_files(selected, index_path.parent)
    paths = [str(index_path.parent / path) for path in selected["relative_path"]]
    events = []
    connection = duckdb.connect(":memory:")
    try:
        connection.execute("SET TimeZone = 'UTC'")
        for start in range(0, len(paths), file_batch_size):
            files = paths[start : start + file_batch_size]
            rows = connection.execute(
                """
                SELECT symbol, epoch_ms(open_time),
                       ((high - low) / open) * 100 AS amplitude_percent
                FROM read_parquet(?, union_by_name=true)
                WHERE timeframe = '1d'
                  AND open > 0
                  AND ((high - low) / open) * 100 > ?
                ORDER BY symbol, open_time
                """,
                [files, threshold_percent],
            ).fetchall()
            for symbol, open_ms, amplitude in rows:
                events.append({
                    "symbol": str(symbol).strip().upper(),
                    "day_ms": int(open_ms),
                    "event_direction": "",
                    "event_amplitude_percent": f"{float(amplitude):.6f}",
                })
    finally:
        connection.close()
    return events


def _load_month_bars(
    index_path: Path,
    symbol: str,
    year: int,
    month: int,
    file_batch_size: int,
) -> dict[int, list[tuple[int, float, float, float, float]]]:
    index = load_archive_index(index_path)
    parts = index[
        index["timeframe"].eq("1m")
        & index["symbol"].eq(symbol)
        & (index["year"] == year)
        & (index["month"] == month)
    ].drop_duplicates("relative_path")
    if parts.empty:
        return {}
    verify_archive_index_files(parts, index_path.parent)
    by_day: dict[int, list[tuple[int, float, float, float, float]]] = defaultdict(list)
    connection = duckdb.connect(":memory:")
    try:
        connection.execute("SET TimeZone = 'UTC'")
        rows = connection.execute(
            """
            SELECT epoch_ms(open_time), open, high, low, close
            FROM read_parquet(?, union_by_name=true)
            WHERE symbol = ?
              AND timeframe = '1m'
              AND epoch_ms(open_time) >= ?
              AND epoch_ms(open_time) < ?
            ORDER BY open_time
            """,
            [[str(index_path.parent / path) for path in parts["relative_path"]],
             symbol,
             int(datetime(year, month, 1, tzinfo=timezone.utc).timestamp() * 1000),
             int(datetime(
                 year + (1 if month == 12 else 0),
                 1 if month == 12 else month + 1,
                 1,
                 tzinfo=timezone.utc,
             ).timestamp() * 1000)],
        ).fetchall()
    finally:
        connection.close()
    for t, open_, high, low, close in rows:
        day_key = t - (t % DAY_MS)
        by_day[day_key].append((int(t), float(open_), float(high), float(low), float(close)))
    return dict(by_day)


def main() -> int:
    args = parse_args()
    if args.zigzag_percent <= 0:
        raise ValueError("zigzag-percent must be positive")
    if args.spike_threshold <= 0:
        raise ValueError("spike-threshold must be positive")
    if args.merge_short_minutes < 0:
        raise ValueError("merge-short-minutes must be non-negative")
    if args.threshold_percent <= 0:
        raise ValueError("threshold-percent must be positive")
    index_path = args.archive_index.resolve()

    events = (
        _report_events(args.report)
        if args.report is not None
        else _scan_1d_events(index_path, args.threshold_percent, args.file_batch_size)
    )
    if not events:
        raise ValueError("no events to process")
    events.sort(key=lambda event: (event["symbol"], event["day_ms"]))

    grouped: dict[tuple[str, int, int], list[dict[str, object]]] = defaultdict(list)
    for event in events:
        opened = datetime.fromtimestamp(event["day_ms"] / 1000, tz=timezone.utc)
        grouped[(event["symbol"], opened.year, opened.month)].append(event)

    output_rows = []
    processed = 0
    for (symbol, year, month), group_events in grouped.items():
        by_day = _load_month_bars(
            index_path, symbol, year, month, args.file_batch_size
        )
        for event in group_events:
            processed += 1
            day_ms = event["day_ms"]
            bars = by_day.get(day_ms)
            if not bars:
                print(f"WARN {symbol} {_iso(day_ms)[:10]}: no 1m data, skipped")
                continue
            pivots = _zigzag_pivots(bars, args.zigzag_percent)
            waves = _build_waves(
                bars, pivots, args.spike_threshold, args.merge_short_minutes
            )
            day_high = max(bar[2] for bar in bars)
            day_low = min(bar[3] for bar in bars)
            for wave in waves:
                wave_start = int(datetime.fromisoformat(wave["start_utc"]).timestamp() * 1000)
                wave_end = int(datetime.fromisoformat(wave["end_utc"]).timestamp() * 1000)
                covers = False
                for bar in bars:
                    if wave_start <= bar[0] <= wave_end:
                        if bar[2] >= day_high or bar[3] <= day_low:
                            covers = True
                            break
                output_rows.append({
                    "symbol": symbol,
                    "event_day_utc": _iso(day_ms)[:10],
                    "event_direction": event["event_direction"],
                    "event_amplitude_percent": event["event_amplitude_percent"],
                    "cycle_index": wave["wave_index"],
                    "cycle_count": len(waves),
                    "category": _classify_cycle(
                        wave["direction"],
                        wave["amplitude_percent"],
                        wave["spike_direction"],
                        args.classify_threshold,
                    ) if args.classify_threshold > 0 else "",
                    **{key: wave[key] for key in (
                        "direction", "start_utc", "end_utc", "duration_minutes",
                        "start_price", "end_price", "amplitude_percent",
                        "range_percent", "high_price", "high_utc", "low_price",
"low_utc", "contains_spike", "spike_direction", "merged",
                )},
                "covers_day_extreme": str(covers).lower(),
            })
        if processed % 100 == 0:
            print(f"progress: {processed}/{len(events)} events processed")

    if args.spike_filter is not None:
        filtered = [row for row in output_rows if row["spike_direction"] == args.spike_filter]
        print(
            f"spike-filter={args.spike_filter} kept={len(filtered)} "
            f"dropped={len(output_rows) - len(filtered)}"
        )
        output_rows = filtered

    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.split_categories:
        for category in CATEGORY_NAMES:
            split_path = args.output.with_name(
                f"{args.output.stem}_{category}{args.output.suffix}"
            )
            rows = [row for row in output_rows if row["category"] == category]
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", newline="", dir=split_path.parent,
                prefix=f".{split_path.name}.", suffix=".tmp", delete=False,
            ) as stream:
                temporary = Path(stream.name)
                writer = csv.DictWriter(stream, fieldnames=OUTPUT_FIELDS)
                writer.writeheader()
                writer.writerows(rows)
            temporary.replace(split_path)
            print(
                f"{category:<11} ({CATEGORY_NAMES[category]}) {len(rows):>5} "
                f"-> {split_path.name}"
            )
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", newline="", dir=args.output.parent,
        prefix=f".{args.output.name}.", suffix=".tmp", delete=False,
    ) as stream:
        temporary = Path(stream.name)
        writer = csv.DictWriter(stream, fieldnames=OUTPUT_FIELDS)
        writer.writeheader()
        writer.writerows(output_rows)
    temporary.replace(args.output)
    print(
        f"events={len(events)} cycles={len(output_rows)} "
        f"output={args.output.resolve()}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
