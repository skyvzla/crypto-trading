from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

import pandas as pd

from .dataset import build_event_dataset, load_bar1s_frame
from .derivatives import attach_derivative_factors, load_metrics_frame
from .event import SpikeEventConfig
from .labels import SpikeLabelConfig
from .lift import render_lift_report
from .workflow import analyze_event_dataset


def _timestamp_ms(value: str) -> int:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        raise argparse.ArgumentTypeError("time must include timezone")
    return int(timestamp.timestamp() * 1_000)


def _symbols(value: str) -> tuple[str, ...]:
    symbols = tuple(
        dict.fromkeys(item.strip().upper() for item in value.split(",") if item.strip())
    )
    if not symbols:
        raise argparse.ArgumentTypeError("symbols must not be empty")
    return symbols


def _write_dataset(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() == ".csv":
        frame.to_csv(path, index=False)
        return
    if path.suffix.lower() in {".parquet", ".pq"}:
        frame.to_parquet(path, index=False, compression="zstd")
        return
    raise ValueError("dataset output must use .csv, .parquet or .pq")


def _chunk_ranges(start_ms: int, end_ms: int, chunk_hours: float) -> list[tuple[int, int]]:
    if chunk_hours <= 0:
        raise ValueError("chunk_hours must be positive")
    chunk_ms = max(1_000, int(chunk_hours * 3_600_000))
    ranges: list[tuple[int, int]] = []
    current = start_ms
    while current < end_ms:
        next_end = min(current + chunk_ms, end_ms)
        ranges.append((current, next_end))
        current = next_end
    return ranges


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build spike event factors from existing archives without widening 1s storage."
    )
    parser.add_argument("catalog", type=Path, help="candles DuckDB catalog")
    parser.add_argument("--symbols", required=True, type=_symbols)
    parser.add_argument("--start", required=True, type=_timestamp_ms)
    parser.add_argument("--end", required=True, type=_timestamp_ms)
    parser.add_argument("--metrics-catalog", type=Path, default=None)
    parser.add_argument("--rise-threshold", type=float, default=0.05)
    parser.add_argument("--volume-multiple-threshold", type=float, default=5.0)
    parser.add_argument("--cooldown-seconds", type=int, default=60)
    parser.add_argument("--target", default="short_mfe_30m")
    parser.add_argument("--correlation-threshold", type=float, default=0.8)
    parser.add_argument(
        "--chunk-hours",
        type=float,
        default=24.0,
        help="read one symbol in bounded time chunks; default 24h",
    )
    parser.add_argument("--include-scale-sensitive", action="store_true")
    parser.add_argument(
        "--lift-report",
        action="store_true",
        help="append base-rate lift / terrain / rule-combination analysis to the report",
    )
    parser.add_argument("--report-out", type=Path, default=None)
    parser.add_argument(
        "--dataset-out",
        type=Path,
        default=None,
        help="optional event-level dataset; omitted by default to save disk",
    )
    args = parser.parse_args(argv)
    if args.start >= args.end:
        parser.error("--start must be earlier than --end")
    if args.chunk_hours <= 0:
        parser.error("--chunk-hours must be positive")

    label_config = SpikeLabelConfig()
    event_config = SpikeEventConfig(
        rise_threshold=args.rise_threshold,
        volume_multiple_threshold=args.volume_multiple_threshold,
        cooldown_seconds=args.cooldown_seconds,
    )
    warmup_ms = max(5 * 60_000, args.cooldown_seconds * 1_000 + 60_000)
    future_ms = max(label_config.horizons_seconds) * 1_000
    event_parts: list[pd.DataFrame] = []
    for symbol in args.symbols:
        for chunk_start, chunk_end in _chunk_ranges(
            args.start, args.end, args.chunk_hours
        ):
            bars = load_bar1s_frame(
                args.catalog,
                symbols=(symbol,),
                start_ms=chunk_start - warmup_ms,
                end_ms=chunk_end + future_ms,
            )
            events = build_event_dataset(
                bars,
                event_config=event_config,
                label_config=label_config,
                event_start_ms=chunk_start,
                event_end_ms=chunk_end,
            )
            if args.metrics_catalog is not None and not events.empty:
                metrics = load_metrics_frame(
                    args.metrics_catalog,
                    symbols=(symbol,),
                    start_ms=chunk_start - 24 * 60 * 60_000,
                    end_ms=chunk_end,
                )
                events = attach_derivative_factors(events, metrics)
            if not events.empty:
                event_parts.append(events)

    dataset = (
        pd.concat(event_parts, ignore_index=True).sort_values(
            ["timestamp_ms", "symbol"], kind="stable"
        ).reset_index(drop=True)
        if event_parts
        else pd.DataFrame()
    )
    result = analyze_event_dataset(
        dataset,
        label_config=label_config,
        target=args.target,
        include_scale_sensitive=args.include_scale_sensitive,
        correlation_threshold=args.correlation_threshold,
    )
    report = result.report
    if args.lift_report:
        factor_names = [spec.name for spec in result.factor_specs]
        report += "\n\n" + render_lift_report(
            result.dataset,
            factor_names,
            target=args.target,
        )

    if args.report_out is None:
        print(report)
    else:
        args.report_out.parent.mkdir(parents=True, exist_ok=True)
        args.report_out.write_text(report, encoding="utf-8")
    if args.dataset_out is not None:
        _write_dataset(result.dataset, args.dataset_out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
