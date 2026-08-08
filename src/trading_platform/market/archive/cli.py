from __future__ import annotations

import argparse
import json
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Sequence

import httpx

from .parquet import ParquetCandleArchive, create_duckdb_catalog
from .vision import (
    BinanceVisionHTTPFetcher,
    DownloadProgress,
    DownloadResult,
    download_history,
)


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
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="concurrent download/parse workers (default: 4)",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="download and replace partitions that already exist",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the final result as JSON",
    )
    args = parser.parse_args(argv)
    if args.attempts < 1:
        parser.error("--attempts must be positive")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    if args.workers < 1:
        parser.error("--workers must be positive")
    reporter = _ProgressReporter(args.workers)
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
                    on_progress=reporter,
                    max_workers=args.workers,
                    overwrite=args.overwrite,
                )
        catalog_path = args.catalog or args.archive / "history.duckdb"
        create_duckdb_catalog(args.archive, catalog_path)
    except Exception as error:
        if args.json:
            print(json.dumps({"status": "failed", "error": str(error)}))
        else:
            print(f"Failed: {error}", file=sys.stderr)
        return 1
    _print_result(results, args.archive, catalog_path, as_json=args.json)
    return 0


class _ProgressReporter:
    def __init__(self, workers: int) -> None:
        self._workers = workers
        self._lock = threading.Lock()
        self._started = False
        self._completed = 0
        self._downloads: dict[int, tuple[str, str]] = {}

    def __call__(self, progress: DownloadProgress) -> None:
        with self._lock:
            if not self._started:
                self._started = True
                print(
                    f"Processing {progress.total} files with "
                    f"{self._workers} workers.",
                    file=sys.stderr,
                    flush=True,
                )
            if progress.phase == "downloaded":
                seconds = max(progress.elapsed_seconds, 1e-9)
                self._downloads[progress.current] = (
                    _format_bytes(progress.downloaded_bytes),
                    _format_bytes(progress.downloaded_bytes / seconds),
                )
                return
            if progress.phase not in {"stored", "skipped"}:
                return
            self._completed += 1
            prefix = (
                f"[{self._completed}/{progress.total}] {progress.symbol} "
                f"{progress.timeframe} {progress.period}"
            )
            if progress.phase == "skipped":
                message = f"{prefix} skipped, already exists ({progress.rows} rows)"
            else:
                size, speed = self._downloads.pop(progress.current, ("?", "?"))
                message = (
                    f"{prefix} stored {progress.rows} rows "
                    f"({size} at {speed}/s)"
                )
            print(message, file=sys.stderr, flush=True)


def _print_result(
    results: Sequence[DownloadResult],
    archive_path: Path,
    catalog_path: Path,
    *,
    as_json: bool,
) -> None:
    rows = sum(item.rows for item in results)
    skipped = sum(item.skipped for item in results)
    if as_json:
        print(
            json.dumps(
                {
                    "status": "complete",
                    "archive": str(archive_path),
                    "catalog": str(catalog_path),
                    "rows": rows,
                    "imports": [
                        {
                            "symbol": item.symbol,
                            "timeframe": item.timeframe,
                            "period": item.period,
                            "rows": item.rows,
                            "skipped": item.skipped,
                        }
                        for item in results
                    ],
                },
                sort_keys=True,
            )
        )
        return
    downloaded = len(results) - skipped
    print(f"Complete: {downloaded} downloaded, {skipped} skipped, {rows} rows.")
    print(f"Archive: {archive_path}")
    print(f"Catalog: {catalog_path}")


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
