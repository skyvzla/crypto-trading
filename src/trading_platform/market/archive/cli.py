from __future__ import annotations

import argparse
from contextlib import ExitStack
import json
import logging
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
from trading_platform.shared.progress import TaskDashboard
from trading_platform.shared.symbol_universe_query import (
    EFFECTIVE_SYMBOL_UNIVERSE_SQL,
)

from .metrics import (
    METRICS_INDEX_FILENAME,
    METRICS_PERIOD,
    MetricsArchive,
    download_metrics_history,
)
from .index import ARCHIVE_INDEX_FILENAME
from .parquet import ParquetCandleArchive, ensure_duckdb_catalog
from .vision import (
    BinanceFuturesMetadataFetcher,
    BinanceVisionHTTPFetcher,
    BinanceVisionWorkerPoolFetcher,
    DownloadProgress,
    DownloadResult,
    current_archive_worker_id,
    download_history,
)

logger = logging.getLogger(__name__)


def _setup_logging(log_level: str, log_file: Path | None) -> None:
    root = logging.getLogger("trading_platform.market.archive")
    root.setLevel(logging.DEBUG)
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(getattr(logging, log_level.upper(), logging.INFO))
    console.setFormatter(formatter)
    root.addHandler(console)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Download verified Binance USD-M candles and metrics into Parquet."
    )
    parser.add_argument("archive", type=Path, help="candles Parquet archive root")
    parser.add_argument(
        "--catalog",
        type=Path,
        default=None,
        help="DuckDB query catalog path (default: <archive>/candles.duckdb)",
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
    parser.add_argument(
        "--metrics-archive",
        type=Path,
        default=None,
        help="metrics Parquet archive root (default: sibling metrics/ directory)",
    )
    parser.add_argument(
        "--without-metrics",
        action="store_true",
        help="skip the default USD-M metrics download",
    )
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
    parser.add_argument(
        "--log-level",
        type=str,
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="console log level for retries/routes (default: INFO)",
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        default=None,
        help="write full DEBUG logs to this file (default: logs/market_archive_<ts>.log)",
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
    main_worker = "worker=main"
    log_file = args.log_file or Path(
        f"logs/market_archive_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    )
    _setup_logging(args.log_level, log_file)
    try:
        candle_reporter = _ProgressReporter(workers)
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
        candle_storage_guard = _DiskSpaceGuard(args.archive, args.min_free_gb)
        candle_storage_guard()
        _require_index_for_existing_archive(
            args.archive,
            ARCHIVE_INDEX_FILENAME,
            dataset_label="candles",
        )
        metrics_archive_path = None
        metrics_storage_guard = None
        if not args.without_metrics:
            metrics_archive_path = (
                args.metrics_archive or args.archive.resolve().parent / "metrics"
            )
            _validate_distinct_archive_roots(args.archive, metrics_archive_path)
            metrics_storage_guard = _DiskSpaceGuard(
                metrics_archive_path, args.min_free_gb
            )
            metrics_storage_guard()
            _require_index_for_existing_archive(
                metrics_archive_path,
                METRICS_INDEX_FILENAME,
                dataset_label="metrics",
            )
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
                    on_retry=candle_reporter.retry,
                )(symbols)
            except Exception as error:
                candle_reporter.metadata_fallback(error)
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
                    on_retry=candle_reporter.retry,
                    on_route=candle_reporter.route,
                )
            else:
                fetch = BinanceVisionHTTPFetcher(
                    clients[0],
                    attempts=args.attempts,
                    on_retry=candle_reporter.retry,
                )
            metrics_results: list[DownloadResult] = []
            metrics_catalog_path = None
            metrics_reporter: _ProgressReporter | None = None
            if metrics_archive_path is None:
                with ParquetCandleArchive(
                    args.archive, index_workers=min(workers, 8)
                ) as archive:
                    candle_results = download_history(
                        archive,
                        fetch=fetch,
                        symbols=symbols,
                        timeframes=args.timeframes,
                        start=start,
                        end=end,
                        on_progress=candle_reporter,
                        max_workers=workers,
                        overwrite=args.overwrite,
                        symbol_availability=symbol_availability,
                        storage_check=candle_storage_guard,
                        on_worker_exit=candle_reporter.worker_exit,
                    )
            else:
                metrics_reporter = _ProgressReporter(workers)
                with (
                    ParquetCandleArchive(
                        args.archive, index_workers=min(workers, 8)
                    ) as archive,
                    MetricsArchive(
                        metrics_archive_path,
                        index_workers=min(workers, 8),
                    ) as metrics_archive,
                ):
                    candle_kwargs = {
                        "fetch": fetch,
                        "symbols": symbols,
                        "timeframes": args.timeframes,
                        "start": start,
                        "end": end,
                        "on_progress": candle_reporter,
                        "max_workers": workers,
                        "overwrite": args.overwrite,
                        "symbol_availability": symbol_availability,
                        "storage_check": candle_storage_guard,
                        "on_worker_exit": candle_reporter.worker_exit,
                    }
                    metrics_kwargs = {
                        "fetch": fetch,
                        "symbols": symbols,
                        "start": start,
                        "end": end,
                        "on_progress": metrics_reporter,
                        "max_workers": workers,
                        "overwrite": args.overwrite,
                        "symbol_availability": symbol_availability,
                        "storage_check": metrics_storage_guard,
                        "on_worker_exit": metrics_reporter.worker_exit,
                    }
                    candle_results = download_history(archive, **candle_kwargs)
                    metrics_results = download_metrics_history(
                        metrics_archive,
                        **metrics_kwargs,
                    )
                    metrics_catalog_path = metrics_archive_path / "metrics.duckdb"
                    metrics_archive.publish(metrics_catalog_path)
                    metrics_reporter.close()
        catalog_path = args.catalog or args.archive / "candles.duckdb"
        ensure_duckdb_catalog(args.archive, catalog_path)
    except KeyboardInterrupt:
        candle_reporter.close(status="interrupted")
        print(f"{main_worker} Cancelled; downloader exiting.", file=sys.stderr)
        return 130
    except Exception as error:
        candle_reporter.close(status="failed")
        if args.json:
            print(json.dumps({"status": "failed", "error": str(error)}))
        else:
            print(
                f"{main_worker} Failed: {error}; downloader exiting.",
                file=sys.stderr,
            )
        return 1
    candle_reporter.close()
    _print_result(
        candle_results,
        args.archive,
        catalog_path,
        metrics_results=metrics_results,
        metrics_archive_path=metrics_archive_path,
        metrics_catalog_path=metrics_catalog_path,
        as_json=args.json,
    )
    return 0


def _require_index_for_existing_archive(
    root: Path,
    index_filename: str,
    *,
    dataset_label: str,
) -> None:
    archive_root = root.resolve()
    if (archive_root / index_filename).is_file() or not archive_root.is_dir():
        return
    if any(entry.is_dir() for entry in archive_root.iterdir()):
        raise RuntimeError(
            f"{dataset_label} archive has data but no index; "
            "run market-archive-index before downloading"
        )


def _validate_distinct_archive_roots(
    candles_archive: Path, metrics_archive: Path
) -> None:
    candles_root = candles_archive.resolve()
    metrics_root = metrics_archive.resolve()
    if candles_root == metrics_root:
        raise ValueError("metrics archive must be separate from the candles archive")
    try:
        metrics_root.relative_to(candles_root)
    except ValueError:
        pass
    else:
        raise ValueError("metrics archive cannot be nested under the candles archive")
    try:
        candles_root.relative_to(metrics_root)
    except ValueError:
        return
    raise ValueError("candles archive cannot be nested under the metrics archive")


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
    """将 worker 下载进度汇总到 TaskDashboard，细节写入日志文件。"""

    def __init__(self, workers: int, *, title: str = "market-archive") -> None:
        self._workers = workers
        self._title = title
        self._lock = threading.Lock()
        self._dashboard: TaskDashboard | None = None
        self._active_names: dict[int, str] = {}

    def _ensure_dashboard(self, total: int) -> TaskDashboard:
        if self._dashboard is None:
            self._dashboard = TaskDashboard(
                title=self._title,
                total=total,
                stream=sys.stderr,
            )
            self._dashboard.start(detail=f"workers={self._workers}")
        return self._dashboard

    def _task_name(self, progress: DownloadProgress) -> str:
        return (
            f"w{progress.worker_id} {progress.symbol} "
            f"{progress.timeframe} {progress.period}"
        )

    def __call__(self, progress: DownloadProgress) -> None:
        with self._lock:
            if progress.phase == "downloaded":
                self._ensure_dashboard(progress.total)
                name = self._task_name(progress)
                self._active_names[progress.worker_id] = name
                self._dashboard.task_start(name)
                return
            if progress.phase not in {
                "stored",
                "skipped",
                "unavailable",
                "failed",
            }:
                return
            dashboard = self._ensure_dashboard(progress.total)
            name = self._active_names.pop(progress.worker_id, None) or (
                self._task_name(progress)
            )
            if progress.phase == "stored":
                logger.debug(
                    f"stored {progress.symbol} {progress.timeframe} "
                    f"{progress.period} rows={progress.rows}"
                    f" elapsed={progress.elapsed_seconds:.1f}s"
                )
                dashboard.task_done(name, "OK")
            elif progress.phase == "skipped":
                logger.debug(
                    f"skipped {progress.symbol} {progress.timeframe} "
                    f"{progress.period} rows={progress.rows}"
                )
                dashboard.task_skip(name)
            elif progress.phase == "unavailable":
                logger.warning(
                    f"unavailable {progress.symbol} {progress.timeframe} "
                    f"{progress.period}"
                )
                dashboard.task_done(name, "Unavailable", count_as_sample=False)
            else:
                logger.error(
                    f"failed {progress.symbol} {progress.timeframe} "
                    f"{progress.period}: {progress.error}"
                )
                dashboard.task_failed(name)

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
            logger.warning(
                f"{worker} Retry {attempt}/{attempts} "
                f"{filename}{source}{duration}: "
                f"{type(error).__name__}: {error}"
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
            logger.info(
                f"{_worker_label(worker_id)} {mode.title()} "
                f"{attempt}/{attempts} {filename} from={previous} "
                f"to={source}{reason_text}"
            )

    def metadata_fallback(self, error: Exception) -> None:
        with self._lock:
            logger.warning(
                f"{_worker_label(None)} Warning: exchangeInfo "
                "unavailable after retries; "
                f"continuing with 404 fallback: {type(error).__name__}: {error}"
            )

    def worker_exit(self, worker_id: int) -> None:
        with self._lock:
            logger.debug(f"{_worker_label(worker_id)} exited")

    def close(self, *, status: str = "ok") -> None:
        with self._lock:
            if self._dashboard is not None:
                self._dashboard.close(status=status)


def _worker_label(worker_id: int | None = None) -> str:
    resolved = current_archive_worker_id() if worker_id is None else worker_id
    return f"worker={resolved}" if resolved > 0 else "worker=main"


def _print_result(
    results: Sequence[DownloadResult],
    archive_path: Path,
    catalog_path: Path,
    *,
    metrics_results: Sequence[DownloadResult] = (),
    metrics_archive_path: Path | None = None,
    metrics_catalog_path: Path | None = None,
    as_json: bool,
) -> None:
    all_results = [*results, *metrics_results]
    rows = sum(item.rows for item in all_results)
    skipped = sum(item.skipped for item in all_results)
    unavailable = sum(item.unavailable for item in all_results)
    if as_json:
        print(
            json.dumps(
                {
                    "status": "complete",
                    "archive": str(archive_path),
                    "catalog": str(catalog_path),
                    "metrics_archive": (
                        str(metrics_archive_path)
                        if metrics_archive_path is not None
                        else None
                    ),
                    "metrics_catalog": (
                        str(metrics_catalog_path)
                        if metrics_catalog_path is not None
                        else None
                    ),
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
                    ] + [
                        {
                            "symbol": item.symbol,
                            "dataset": "metrics",
                            "period": METRICS_PERIOD,
                            "partition_date": item.period,
                            "rows": item.rows,
                            "skipped": item.skipped,
                            "unavailable": item.unavailable,
                        }
                        for item in metrics_results
                    ],
                },
                sort_keys=True,
            )
        )
        return
    downloaded = len(all_results) - skipped - unavailable
    print(
        f"Complete: {downloaded} downloaded, {skipped} existing, "
        f"{unavailable} unavailable, {rows} rows."
    )
    print(f"Archive: {archive_path}")
    print(f"Catalog: {catalog_path}")
    if metrics_archive_path is not None and metrics_catalog_path is not None:
        print(f"Metrics archive: {metrics_archive_path}")
        print(f"Metrics catalog: {metrics_catalog_path}")


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
