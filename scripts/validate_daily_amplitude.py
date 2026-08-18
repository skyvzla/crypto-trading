#!/usr/bin/env python3
"""独立校验日线异动扫描结果的结构与分钟级时间边界。"""

from __future__ import annotations

import argparse
import csv
from datetime import datetime
from pathlib import Path


REQUIRED_COLUMNS = {
    "symbol",
    "open_time_utc",
    "close_time_utc",
    "amplitude_percent",
    "scan_start_after_utc",
    "new_listing_within_15_days",
    "oscillation_direction",
    "oscillation_start_utc",
    "oscillation_end_utc",
    "oscillation_duration_minutes",
    "minute_data_status",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="校验日线异动 CSV 的不变量")
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("reports/daily_amplitude_over_50pct.csv"),
    )
    parser.add_argument("--threshold-percent", type=float, default=50.0)
    parser.add_argument("--delay-minutes", type=int, default=45)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    with args.report.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        headers = set(reader.fieldnames or ())
        missing_columns = REQUIRED_COLUMNS - headers
        if missing_columns:
            raise ValueError(f"missing columns: {', '.join(sorted(missing_columns))}")
        rows = list(reader)
    if not rows:
        raise ValueError("report contains no events")

    errors: list[str] = []
    keys: set[tuple[str, str]] = set()
    resolved = 0
    missing_minutes = 0
    expected_delay_seconds = args.delay_minutes * 60
    for row_number, row in enumerate(rows, start=2):
        key = (row["symbol"], row["open_time_utc"])
        if key in keys:
            errors.append(f"row {row_number}: duplicate symbol/day {key}")
        keys.add(key)
        if float(row["amplitude_percent"]) <= args.threshold_percent:
            errors.append(f"row {row_number}: threshold is not strictly exceeded")
        close_time = datetime.fromisoformat(row["close_time_utc"])
        scan_start = datetime.fromisoformat(row["scan_start_after_utc"])
        if round((scan_start - close_time).total_seconds()) != expected_delay_seconds:
            errors.append(f"row {row_number}: unexpected delayed start")
        status = row["minute_data_status"]
        if status == "missing_1m":
            missing_minutes += 1
            continue
        if status == "ambiguous_same_minute":
            if row["oscillation_direction"] != "ambiguous":
                errors.append(f"row {row_number}: ambiguous minute status has wrong direction")
            continue
        if status not in {
            "resolved",
            "partial_listing_day",
            "partial_1m",
            "incomplete_1m",
            "minute_extreme_mismatch",
        }:
            errors.append(f"row {row_number}: unknown minute status {status}")
            continue
        resolved += 1
        start_time = datetime.fromisoformat(row["oscillation_start_utc"])
        end_time = datetime.fromisoformat(row["oscillation_end_utc"])
        day_open = datetime.fromisoformat(row["open_time_utc"])
        if not day_open <= start_time <= end_time <= close_time:
            errors.append(f"row {row_number}: minute bounds are outside the daily candle")
        expected_duration = round((end_time - start_time).total_seconds() / 60)
        if int(row["oscillation_duration_minutes"]) != expected_duration:
            errors.append(f"row {row_number}: duration does not match timestamps")
        if row["oscillation_direction"] not in {"upward", "downward"}:
            errors.append(f"row {row_number}: unknown direction")

    if errors:
        print("FAILED")
        print("\n".join(errors[:50]))
        return 1
    print(
        f"OK rows={len(rows)} unique_symbol_days={len(keys)} "
        f"resolved_minutes={resolved} missing_minutes={missing_minutes}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
