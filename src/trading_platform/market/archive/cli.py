from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Sequence

import httpx

from .parquet import ParquetCandleArchive, create_duckdb_catalog
from .vision import BinanceVisionHTTPFetcher, DownloadProgress, download_history


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Download verified Binance USD-M history into Parquet."
    )
    parser.add_argument("archive", type=Path, help="Parquet archive root")
    parser.add_argument(
        "--catalog",
        type=Path,
        default=None,
        help="DuckDB query catalog path (default: <archive>/history.duckdb)",
    )
    parser.add_argument("--symbols", nargs="+", required=True)
    parser.add_argument("--timeframes", nargs="+", required=True)
    parser.add_argument("--start", required=True, help="ISO 8601 inclusive UTC time")
    parser.add_argument("--end", required=True, help="ISO 8601 exclusive UTC time")
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=60.0)
    args = parser.parse_args(argv)
    if args.attempts < 1:
        parser.error("--attempts must be positive")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    try:
        start = _parse_datetime(args.start)
        end = _parse_datetime(args.end)
        with httpx.Client(timeout=args.timeout, follow_redirects=True) as client:
            fetch = BinanceVisionHTTPFetcher(client, attempts=args.attempts)
            with ParquetCandleArchive(args.archive) as archive:
                results = download_history(
                    archive,
                    fetch=fetch,
                    symbols=args.symbols,
                    timeframes=args.timeframes,
                    start=start,
                    end=end,
                    on_progress=_print_progress,
                )
        catalog_path = args.catalog or args.archive / "history.duckdb"
        create_duckdb_catalog(args.archive, catalog_path)
    except Exception as error:
        print(json.dumps({"status": "failed", "error": str(error)}))
        return 1
    print(
        json.dumps(
            {
                "status": "complete",
                "archive": str(args.archive),
                "catalog": str(catalog_path),
                "rows": sum(item.rows for item in results),
                "imports": [
                    {
                        "symbol": item.symbol,
                        "timeframe": item.timeframe,
                        "period": item.period,
                        "rows": item.rows,
                    }
                    for item in results
                ],
            },
            sort_keys=True,
        )
    )
    return 0


def _print_progress(progress: DownloadProgress) -> None:
    prefix = (
        f"[{progress.current}/{progress.total}] {progress.symbol} "
        f"{progress.timeframe} {progress.period}"
    )
    if progress.phase == "downloaded":
        seconds = max(progress.elapsed_seconds, 1e-9)
        size = _format_bytes(progress.downloaded_bytes)
        speed = _format_bytes(progress.downloaded_bytes / seconds)
        message = f"{prefix} downloaded {size} ({speed}/s)"
    elif progress.phase == "stored":
        message = f"{prefix} stored {progress.rows} rows"
    else:
        message = f"{prefix} {progress.phase}"
    print(message, file=sys.stderr, flush=True)


def _format_bytes(value: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.1f} {unit}"
        value /= 1024
    raise AssertionError("unreachable")


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("start and end must include a timezone")
    return parsed.astimezone(UTC)
