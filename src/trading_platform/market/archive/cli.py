from __future__ import annotations

import argparse
from contextlib import ExitStack
import json
import os
import shutil
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Sequence

import httpx
import psycopg

from trading_platform.shared.config import DatabaseConfig
from trading_platform.shared.symbol_universe_query import (
    EFFECTIVE_SYMBOL_UNIVERSE_SQL,
)

from .parquet import ParquetCandleArchive, create_duckdb_catalog
from .vision import (
    BinanceFuturesMetadataFetcher,
    BinanceVisionHTTPFetcher,
    BinanceVisionWorkerPoolFetcher,
    DownloadProgress,
    DownloadResult,
    current_archive_worker_id,
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
    symbol_source = parser.add_mutually_exclusive_group()
    symbol_source.add_argument(
        "--symbols",
        nargs="+",
        help="symbols to download; omit to load all tradable symbols from PostgreSQL",
    )
    symbol_source.add_argument(
        "--all-symbols",
        action="store_true",
        help="load all currently tradable symbols from PostgreSQL",
    )
    parser.add_argument(
        "--dsn",
        help="PostgreSQL DSN for automatic symbol selection (default: DB_* settings)",
    )
    parser.add_argument(
        "--delisting-freeze-days",
        type=int,
        default=os.getenv("SPIKE_DELISTING_FREEZE_DAYS", "15"),
        help="exclude symbols delivering within this many days (default: 15)",
    )
    parser.add_argument(
        "--strategy-id",
        help="also apply optional category switches configured for this strategy",
    )
    parser.add_argument(
        "--min-free-gb",
        type=float,
        default=os.getenv("MARKET_HISTORY_MIN_FREE_GB", "10"),
        help="stop when archive filesystem free space reaches this value; 0 disables",
    )
    parser.add_argument("--timeframes", nargs="+", required=True)
    parser.add_argument("--start", required=True, help="ISO 8601 inclusive UTC time")
    parser.add_argument("--end", required=True, help="ISO 8601 exclusive UTC time")
    parser.add_argument("--attempts", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument(
        "--workers",
        type=int,
        default=None,
        help="concurrent workers (default: proxy count, otherwise 4)",
    )
    parser.add_argument(
        "--proxy",
        dest="proxies",
        action="append",
        help=(
            "HTTP(S) proxy URL; repeat for failover download proxies "
            "(or set MARKET_HISTORY_PROXIES as a comma-separated list)"
        ),
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
    proxies = args.proxies
    if proxies is None:
        proxies = _parse_proxy_environment(
            os.getenv("MARKET_HISTORY_PROXIES", "")
        )
    else:
        proxies = [proxy.strip() for proxy in proxies if proxy.strip()]
    _validate_proxies(parser, proxies)
    workers = args.workers
    if workers is None:
        workers = len(proxies) if proxies else 4
    if args.attempts < 1:
        parser.error("--attempts must be positive")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    if workers < 1:
        parser.error("--workers must be positive")
    if args.delisting_freeze_days < 0:
        parser.error("--delisting-freeze-days must be non-negative")
    if args.min_free_gb < 0:
        parser.error("--min-free-gb must be non-negative")
    reporter = _ProgressReporter(workers)
    main_worker = "worker=main"
    try:
        start = _parse_datetime(args.start)
        end = _parse_datetime(args.end)
        symbols = args.symbols or _load_allowed_symbols(
            args.dsn or DatabaseConfig().dsn,
            freeze_days=args.delisting_freeze_days,
            strategy_id=args.strategy_id,
        )
        if args.symbols is None:
            print(
                f"{main_worker} Loaded {len(symbols)} tradable symbols "
                "from PostgreSQL.",
                file=sys.stderr,
                flush=True,
            )
        symbol_count = len(
            {symbol.strip().upper() for symbol in symbols if symbol.strip()}
        )
        symbol_label = "trading pair" if symbol_count == 1 else "trading pairs"
        print(
            f"{main_worker} Downloading data for {symbol_count} {symbol_label}.",
            file=sys.stderr,
            flush=True,
        )
        if proxies:
            print(
                f"{main_worker} Using {len(proxies)} failover proxies with "
                f"{workers} workers "
                "and direct fallback.",
                file=sys.stderr,
                flush=True,
            )
        storage_guard = _DiskSpaceGuard(args.archive, args.min_free_gb)
        storage_guard()
        with ExitStack() as stack:
            if proxies:
                metadata_client = stack.enter_context(
                    httpx.Client(
                        timeout=args.timeout,
                        follow_redirects=True,
                        trust_env=False,
                    )
                )
                clients = [
                    stack.enter_context(
                        httpx.Client(
                            timeout=args.timeout,
                            follow_redirects=True,
                            proxy=proxy,
                            trust_env=False,
                        )
                    )
                    for proxy in proxies
                ]
            else:
                clients = [
                    stack.enter_context(
                        httpx.Client(
                            timeout=args.timeout,
                            follow_redirects=True,
                        )
                    )
                ]
                metadata_client = clients[0]
            try:
                symbol_availability = BinanceFuturesMetadataFetcher(
                    metadata_client,
                    attempts=args.attempts,
                    on_retry=reporter.retry,
                )(symbols)
            except Exception as error:
                reporter.metadata_fallback(error)
                symbol_availability = {}
            if proxies:
                worker_fetchers = [
                    BinanceVisionHTTPFetcher(
                        client,
                        attempts=1,
                    )
                    for client in clients
                ]
                fetch = BinanceVisionWorkerPoolFetcher(
                    worker_fetchers,
                    direct_fetcher=BinanceVisionHTTPFetcher(
                        metadata_client,
                        attempts=1,
                    ),
                    attempts=args.attempts,
                    labels=[_proxy_label(proxy) for proxy in proxies],
                    on_retry=reporter.retry,
                    on_route=reporter.route,
                )
            else:
                fetch = BinanceVisionHTTPFetcher(
                    clients[0],
                    attempts=args.attempts,
                    on_retry=reporter.retry,
                )
            with ParquetCandleArchive(args.archive) as archive:
                results = download_history(
                    archive,
                    fetch=fetch,
                    symbols=symbols,
                    timeframes=args.timeframes,
                    start=start,
                    end=end,
                    on_progress=reporter,
                    max_workers=workers,
                    overwrite=args.overwrite,
                    symbol_availability=symbol_availability,
                    storage_check=storage_guard,
                    on_worker_exit=reporter.worker_exit,
                )
        catalog_path = args.catalog or args.archive / "history.duckdb"
        create_duckdb_catalog(args.archive, catalog_path)
    except KeyboardInterrupt:
        print(f"{main_worker} Cancelled; downloader exiting.", file=sys.stderr)
        return 130
    except Exception as error:
        if args.json:
            print(json.dumps({"status": "failed", "error": str(error)}))
        else:
            print(
                f"{main_worker} Failed: {error}; downloader exiting.",
                file=sys.stderr,
            )
        return 1
    _print_result(results, args.archive, catalog_path, as_json=args.json)
    return 0


def _parse_proxy_environment(value: str) -> list[str]:
    return [
        item.strip()
        for item in value.replace("\n", ",").split(",")
        if item.strip()
    ]


def _validate_proxies(parser: argparse.ArgumentParser, proxies: Sequence[str]) -> None:
    for proxy in proxies:
        try:
            parsed = httpx.URL(proxy)
        except httpx.InvalidURL:
            parser.error("invalid proxy URL")
        if (
            parsed.scheme not in {"http", "https", "socks5", "socks5h"}
            or not parsed.host
        ):
            parser.error("proxy must be an HTTP(S) or SOCKS5 URL")


def _proxy_label(proxy: str) -> str:
    parsed = httpx.URL(proxy)
    authority = parsed.host or "unknown"
    if parsed.port is not None:
        authority = f"{authority}:{parsed.port}"
    return f"{parsed.scheme}://{authority}"


class _DiskSpaceGuard:
    def __init__(self, path: Path, min_free_gb: float) -> None:
        self.path = path.resolve()
        self.min_free_bytes = int(min_free_gb * 1024**3)
        self._stopped = threading.Event()

    def __call__(self) -> None:
        if self.min_free_bytes == 0:
            return
        if self._stopped.is_set():
            raise RuntimeError("download stopped by the disk space guard")
        target = self.path
        while not target.exists():
            target = target.parent
        free_bytes = shutil.disk_usage(target).free
        if free_bytes <= self.min_free_bytes:
            self._stopped.set()
            raise RuntimeError(
                "insufficient disk space: "
                f"{_format_bytes(free_bytes)} free on {target}, "
                f"requires more than {_format_bytes(self.min_free_bytes)}"
            )


def _load_allowed_symbols(
    dsn: str, *, freeze_days: int, strategy_id: str | None = None
) -> list[str]:
    normalized_strategy = strategy_id.strip() if strategy_id else None
    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                EFFECTIVE_SYMBOL_UNIVERSE_SQL,
                (
                    timedelta(days=freeze_days),
                    normalized_strategy,
                    normalized_strategy,
                ),
            )
            symbols = [str(row[0]).strip().upper() for row in cursor.fetchall()]
    if not symbols:
        raise RuntimeError("PostgreSQL contains no currently tradable symbols")
    return symbols


class _ProgressReporter:
    def __init__(self, workers: int) -> None:
        self._workers = workers
        self._lock = threading.Lock()
        self._started = False
        self._completed = 0
        self._downloads: dict[int, tuple[str, str]] = {}

    def __call__(self, progress: DownloadProgress) -> None:
        with self._lock:
            worker = f"worker={progress.worker_id}"
            if not self._started:
                self._started = True
                print(
                    f"{worker} Processing {progress.total} files with "
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
            if progress.phase not in {
                "stored",
                "skipped",
                "unavailable",
                "failed",
            }:
                return
            self._completed += 1
            duration = _format_duration(progress.elapsed_seconds)
            prefix = (
                f"{worker} [{self._completed}/{progress.total}] "
                f"{progress.symbol} "
                f"{progress.timeframe} {progress.period}"
            )
            if progress.phase == "skipped":
                message = (
                    f"{prefix} skipped, already exists ({progress.rows} rows, "
                    f"{duration})"
                )
            elif progress.phase == "unavailable":
                message = f"{prefix} unavailable (404), skipped ({duration})"
            elif progress.phase == "failed":
                message = (
                    f"{prefix} failed ({duration}): {progress.error}; "
                    "task exiting"
                )
            else:
                size, speed = self._downloads.pop(progress.current, ("?", "?"))
                download = _format_duration(progress.download_seconds)
                processing = _format_duration(progress.processing_seconds)
                message = (
                    f"{prefix} stored {progress.rows} rows "
                    f"({size} at {speed}/s, download={download}, "
                    f"process={processing}, total={duration})"
                )
            print(message, file=sys.stderr, flush=True)

    def retry(
        self,
        url: str,
        attempt: int,
        attempts: int,
        error: Exception,
        *,
        proxy: str | None = None,
        elapsed_seconds: float | None = None,
        worker_id: int | None = None,
    ) -> None:
        with self._lock:
            worker = _worker_label(worker_id)
            filename = url.rsplit("/", 1)[-1]
            source = f" proxy={proxy}" if proxy is not None else ""
            duration = (
                f" elapsed={_format_duration(elapsed_seconds)}"
                if elapsed_seconds is not None
                else ""
            )
            print(
                f"{worker} Retry {attempt}/{attempts} "
                f"{filename}{source}{duration}: "
                f"{type(error).__name__}: {error}",
                file=sys.stderr,
                flush=True,
            )

    def route(
        self,
        url: str,
        attempt: int,
        attempts: int,
        mode: str,
        source: str,
        *,
        previous_source: str | None = None,
        reason: str | None = None,
        worker_id: int | None = None,
    ) -> None:
        with self._lock:
            filename = url.rsplit("/", 1)[-1]
            previous = previous_source or "none"
            reason_text = f" reason={reason}" if reason is not None else ""
            print(
                f"{_worker_label(worker_id)} {mode.title()} "
                f"{attempt}/{attempts} {filename} from={previous} "
                f"to={source}{reason_text}",
                file=sys.stderr,
                flush=True,
            )

    def metadata_fallback(self, error: Exception) -> None:
        with self._lock:
            print(
                "worker=main Warning: exchangeInfo "
                "unavailable after retries; "
                f"continuing with 404 fallback: {type(error).__name__}: {error}",
                file=sys.stderr,
                flush=True,
            )

    def worker_exit(self, worker_id: int) -> None:
        with self._lock:
            print(
                f"worker={worker_id} exited",
                file=sys.stderr,
                flush=True,
            )


def _worker_label(worker_id: int | None = None) -> str:
    resolved = current_archive_worker_id() if worker_id is None else worker_id
    return f"worker={resolved}" if resolved > 0 else "worker=main"


def _print_result(
    results: Sequence[DownloadResult],
    archive_path: Path,
    catalog_path: Path,
    *,
    as_json: bool,
) -> None:
    rows = sum(item.rows for item in results)
    skipped = sum(item.skipped for item in results)
    unavailable = sum(item.unavailable for item in results)
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
                            "unavailable": item.unavailable,
                        }
                        for item in results
                    ],
                },
                sort_keys=True,
            )
        )
        return
    downloaded = len(results) - skipped - unavailable
    print(
        f"Complete: {downloaded} downloaded, {skipped} existing, "
        f"{unavailable} unavailable, {rows} rows."
    )
    print(f"Archive: {archive_path}")
    print(f"Catalog: {catalog_path}")


def _format_bytes(value: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.1f} {unit}"
        value /= 1024
    raise AssertionError("unreachable")


def _format_duration(seconds: float) -> str:
    total_seconds = max(0, round(seconds))
    minutes, remaining = divmod(total_seconds, 60)
    if minutes:
        return f"{minutes}m{remaining:02d}s"
    return f"{remaining}s"


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("start and end must include a timezone")
    return parsed.astimezone(UTC)
