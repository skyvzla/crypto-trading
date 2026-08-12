from __future__ import annotations

import csv
import fcntl
import io
import json
import math
import os
import time
import zipfile
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from threading import Lock, local
from uuid import uuid4

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from trading_platform.market.archive.vision import (
    ArchiveNotFoundError,
    DownloadProgress,
    DownloadResult,
    SymbolAvailability,
    VISION_ROOT,
    current_archive_worker_id,
    open_fetched_archive,
)


METRICS_PERIOD = "5m"
METRICS_INTERVAL = timedelta(minutes=5)
METRICS_INDEX_FILENAME = "metrics_index.parquet"
METRICS_INDEX_META_FILENAME = "metrics_index.meta.json"
METRICS_INDEX_SCHEMA_VERSION = 1
METRICS_SCHEMA_VERSION = 1
METRICS_HEADER = (
    "create_time",
    "symbol",
    "sum_open_interest",
    "sum_open_interest_value",
    "count_toptrader_long_short_ratio",
    "sum_toptrader_long_short_ratio",
    "count_long_short_ratio",
    "sum_taker_long_short_vol_ratio",
)

METRICS_SCHEMA = pa.schema([
    ("symbol", pa.string()),
    ("period", pa.string()),
    ("snapshot_time", pa.timestamp("ms", tz="UTC")),
    ("available_time", pa.timestamp("ms", tz="UTC")),
    ("sum_open_interest", pa.float64()),
    ("sum_open_interest_value", pa.float64()),
    ("count_toptrader_long_short_ratio", pa.float64()),
    ("sum_toptrader_long_short_ratio", pa.float64()),
    ("count_long_short_ratio", pa.float64()),
    ("sum_taker_long_short_vol_ratio", pa.float64()),
    ("source", pa.string()),
    ("quality_status", pa.string()),
    ("schema_version", pa.int16()),
])

METRICS_INDEX_SCHEMA = pa.schema([
    ("market", pa.string()),
    ("dataset", pa.string()),
    ("symbol", pa.string()),
    ("period", pa.string()),
    ("year", pa.int16()),
    ("month", pa.int8()),
    ("day", pa.int8()),
    ("relative_path", pa.string()),
    ("row_count", pa.int64()),
    ("first_snapshot_ms", pa.int64()),
    ("last_snapshot_ms", pa.int64()),
    ("file_size", pa.int64()),
    ("file_mtime_ns", pa.int64()),
])
_WORKER_CONTEXT = local()


class MetricsArchiveIndexError(RuntimeError):
    pass


@dataclass(frozen=True)
class MetricsSnapshot:
    """One normalized Binance USD-M metrics sample."""

    symbol: str
    snapshot_time: datetime
    sum_open_interest: float | None
    sum_open_interest_value: float | None
    count_toptrader_long_short_ratio: float | None
    sum_toptrader_long_short_ratio: float | None
    count_long_short_ratio: float | None
    sum_taker_long_short_vol_ratio: float | None
    available_time: datetime | None = None
    source: str = "binance_vision"

    def __post_init__(self) -> None:
        if self.snapshot_time.tzinfo is None or self.snapshot_time.utcoffset() is None:
            raise ValueError("metrics snapshot timestamp must include a timezone")
        timestamp = self.snapshot_time.astimezone(UTC)
        if timestamp.second or timestamp.microsecond or timestamp.minute % 5:
            raise ValueError("metrics snapshot timestamp must align to a UTC 5m boundary")
        object.__setattr__(self, "snapshot_time", timestamp)
        available = self.available_time or timestamp + METRICS_INTERVAL
        if available.tzinfo is None or available.utcoffset() is None:
            raise ValueError("metrics available timestamp must include a timezone")
        available = available.astimezone(UTC)
        if available < timestamp:
            raise ValueError("metrics available timestamp cannot precede snapshot")
        object.__setattr__(self, "available_time", available)
        object.__setattr__(self, "symbol", self.symbol.strip().upper())
        if not self.symbol:
            raise ValueError("metrics symbol must not be empty")
        if not self.source:
            raise ValueError("metrics source must not be empty")

    @property
    def quality_status(self) -> str:
        fields = (
            self.sum_open_interest,
            self.sum_open_interest_value,
            self.count_toptrader_long_short_ratio,
            self.sum_toptrader_long_short_ratio,
            self.count_long_short_ratio,
            self.sum_taker_long_short_vol_ratio,
        )
        return "complete" if all(value is not None for value in fields) else "partial"


class MetricsArchive:
    """Atomically replaced daily USD-M metrics partitions."""

    def __init__(
        self,
        root: str | Path,
        *,
        index_workers: int = 1,
        rebuild_index_on_close: bool = True,
    ) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_workers = index_workers
        self.rebuild_index_on_close = rebuild_index_on_close
        self._dirty = False
        self._closed = False

    def __enter__(self) -> "MetricsArchive":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._dirty and self.rebuild_index_on_close:
            build_metrics_index(self.root, workers=self.index_workers)

    def publish(self, catalog_path: str | Path) -> tuple[Path, Path]:
        """Publish this archive's index and DuckDB catalog as one generation."""

        if self._closed:
            raise RuntimeError("cannot publish a closed metrics archive")
        published = publish_metrics_archive(
            self.root,
            catalog_path,
            workers=self.index_workers,
        )
        self._dirty = False
        return published

    def partition_rows(self, symbol: str, partition_day: date) -> int | None:
        target = self._partition_path(symbol, partition_day)
        if not target.is_file():
            return None
        return pq.ParquetFile(target).metadata.num_rows

    def upsert(self, snapshots: Sequence[MetricsSnapshot]) -> int:
        if not snapshots:
            return 0
        keys = {
            (snapshot.symbol, snapshot.snapshot_time.date()) for snapshot in snapshots
        }
        if len(keys) != 1:
            raise ValueError(
                "one metrics Parquet write must contain exactly one daily partition"
            )
        symbol, partition_day = keys.pop()
        table = pa.table({
            "symbol": pa.array([symbol] * len(snapshots), type=pa.string()),
            "period": pa.array([METRICS_PERIOD] * len(snapshots), type=pa.string()),
            "snapshot_time": pa.array(
                [item.snapshot_time for item in snapshots],
                type=pa.timestamp("ms", tz="UTC"),
            ),
            "available_time": pa.array(
                [item.available_time for item in snapshots],
                type=pa.timestamp("ms", tz="UTC"),
            ),
            "sum_open_interest": pa.array(
                [item.sum_open_interest for item in snapshots], type=pa.float64()
            ),
            "sum_open_interest_value": pa.array(
                [item.sum_open_interest_value for item in snapshots], type=pa.float64()
            ),
            "count_toptrader_long_short_ratio": pa.array(
                [item.count_toptrader_long_short_ratio for item in snapshots],
                type=pa.float64(),
            ),
            "sum_toptrader_long_short_ratio": pa.array(
                [item.sum_toptrader_long_short_ratio for item in snapshots],
                type=pa.float64(),
            ),
            "count_long_short_ratio": pa.array(
                [item.count_long_short_ratio for item in snapshots], type=pa.float64()
            ),
            "sum_taker_long_short_vol_ratio": pa.array(
                [item.sum_taker_long_short_vol_ratio for item in snapshots],
                type=pa.float64(),
            ),
            "source": pa.array([item.source for item in snapshots], type=pa.string()),
            "quality_status": pa.array(
                [item.quality_status for item in snapshots], type=pa.string()
            ),
            "schema_version": pa.array(
                [METRICS_SCHEMA_VERSION] * len(snapshots), type=pa.int16()
            ),
        })
        return self.upsert_table(table, symbol=symbol, partition_day=partition_day)

    def upsert_table(
        self, table: pa.Table, *, symbol: str, partition_day: date
    ) -> int:
        if not table.num_rows:
            return 0
        if set(table.column_names) != set(METRICS_SCHEMA.names):
            raise ValueError("metrics Arrow table has incompatible columns")
        table = table.select(METRICS_SCHEMA.names).cast(METRICS_SCHEMA)
        # Index/catalog publication holds this lock exclusively; regular
        # downloads hold it shared and still write independent partitions in parallel.
        with _metrics_publish_lock(self.root, exclusive=False):
            partition = self._partition_path(symbol, partition_day).parent
            partition.mkdir(parents=True, exist_ok=True)
            target = partition / "metrics.parquet"
            temporary = partition / f".metrics-{uuid4().hex}.tmp.parquet"
            lock_file = (partition / ".write.lock").open("a+")
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                lock_file.close()
                raise RuntimeError(
                    f"metrics partition writer is already active for {partition}"
                ) from error
            try:
                pq.write_table(table, temporary, compression="zstd")
                os.replace(temporary, target)
                self._dirty = True
            finally:
                temporary.unlink(missing_ok=True)
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                lock_file.close()
        return table.num_rows

    def _partition_path(self, symbol: str, partition_day: date) -> Path:
        return (
            self.root
            / "usdm"
            / symbol.strip().upper()
            / f"{partition_day.year:04d}"
            / f"{partition_day.month:02d}"
            / f"{partition_day.day:02d}"
            / "metrics.parquet"
        )


def metrics_archive_url(symbol: str, day: str) -> str:
    normalized = symbol.strip().upper()
    filename = f"{normalized}-metrics-{day}.zip"
    return f"{VISION_ROOT}/daily/metrics/{normalized}/{filename}"


def parse_metrics_archive(
    content: bytes | io.BufferedIOBase,
    symbol: str,
    partition_day: date | str,
) -> list[MetricsSnapshot]:
    """Parse one Vision daily metrics ZIP without persisting its raw CSV."""

    expected_day = _parse_partition_day(partition_day)
    normalized_symbol = symbol.strip().upper()
    member = f"{normalized_symbol}-metrics-{expected_day.isoformat()}.csv"
    source = io.BytesIO(content) if isinstance(content, bytes) else content
    snapshots: dict[datetime, MetricsSnapshot] = {}
    with zipfile.ZipFile(source) as archive:
        with archive.open(member) as raw:
            rows = csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig"))
            if tuple(rows.fieldnames or ()) != METRICS_HEADER:
                raise ValueError(f"{member} has incompatible columns")
            for row in rows:
                snapshot = _parse_metrics_row(row, normalized_symbol, expected_day, member)
                previous = snapshots.get(snapshot.snapshot_time)
                if previous is None:
                    snapshots[snapshot.snapshot_time] = snapshot
                elif previous != snapshot:
                    raise ValueError(
                        f"{member} has conflicting rows at "
                        f"{snapshot.snapshot_time.isoformat()}"
                    )
    return [snapshots[key] for key in sorted(snapshots)]


def _parse_metrics_row(
    row: Mapping[str, str | None],
    expected_symbol: str,
    expected_day: date,
    member: str,
) -> MetricsSnapshot:
    raw_timestamp = (row["create_time"] or "").strip()
    try:
        snapshot_time = datetime.strptime(raw_timestamp, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=UTC
        )
    except ValueError as error:
        raise ValueError(f"{member} has invalid create_time: {raw_timestamp!r}") from error
    if snapshot_time.date() != expected_day:
        raise ValueError(f"{member} has a row outside its daily partition")
    source_symbol = (row["symbol"] or "").strip().upper()
    if source_symbol != expected_symbol:
        raise ValueError(f"{member} has an unexpected symbol: {source_symbol!r}")
    return MetricsSnapshot(
        symbol=expected_symbol,
        snapshot_time=snapshot_time,
        sum_open_interest=_optional_float(row["sum_open_interest"], member),
        sum_open_interest_value=_optional_float(
            row["sum_open_interest_value"], member
        ),
        count_toptrader_long_short_ratio=_optional_float(
            row["count_toptrader_long_short_ratio"], member
        ),
        sum_toptrader_long_short_ratio=_optional_float(
            row["sum_toptrader_long_short_ratio"], member
        ),
        count_long_short_ratio=_optional_float(row["count_long_short_ratio"], member),
        sum_taker_long_short_vol_ratio=_optional_float(
            row["sum_taker_long_short_vol_ratio"], member
        ),
    )


def _optional_float(value: str | None, member: str) -> float | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        parsed = float(text)
    except ValueError as error:
        raise ValueError(f"{member} has invalid numeric value: {text!r}") from error
    if not math.isfinite(parsed):
        raise ValueError(f"{member} has non-finite numeric value: {text!r}")
    return parsed


def download_metrics_history(
    archive: MetricsArchive,
    *,
    fetch: Callable[[str], bytes],
    symbols: Sequence[str],
    start: datetime,
    end: datetime,
    on_progress: Callable[[DownloadProgress], None] | None = None,
    max_workers: int = 1,
    overwrite: bool = False,
    symbol_availability: Mapping[str, SymbolAvailability] | None = None,
    storage_check: Callable[[], None] | None = None,
    on_worker_exit: Callable[[int], None] | None = None,
) -> list[DownloadResult]:
    """Download verified Vision daily metrics directly into normalized Parquet."""

    start_utc = _require_utc(start)
    end_utc = _require_utc(end)
    if end_utc <= start_utc:
        raise ValueError("end must be later than start")
    normalized_symbols = tuple(
        dict.fromkeys(value.strip().upper() for value in symbols if value.strip())
    )
    if not normalized_symbols:
        raise ValueError("at least one symbol is required")
    if max_workers < 1:
        raise ValueError("max_workers must be positive")

    jobs: list[tuple[str, date]] = []
    for symbol in normalized_symbols:
        availability = (symbol_availability or {}).get(symbol)
        for partition_day in _days(start_utc, end_utc):
            partition_start = datetime.combine(partition_day, datetime.min.time(), UTC)
            partition_end = partition_start + timedelta(days=1)
            if availability is not None and not availability.intersects(
                max(partition_start, start_utc), min(partition_end, end_utc)
            ):
                continue
            jobs.append((symbol, partition_day))
    total = len(jobs)

    def run_process(
        job: tuple[int, tuple[str, date]], task_started: float
    ) -> DownloadResult:
        current, (symbol, partition_day) = job
        label = partition_day.isoformat()
        existing_rows = archive.partition_rows(symbol, partition_day)
        if existing_rows is not None and not overwrite:
            _notify(
                on_progress, "skipped", current, total, symbol, label,
                elapsed_seconds=time.monotonic() - task_started, rows=existing_rows,
            )
            return DownloadResult("%s" % symbol, "metrics", label, existing_rows, skipped=True)
        if storage_check is not None:
            storage_check()
        _notify(on_progress, "downloading", current, total, symbol, label)
        url = metrics_archive_url(symbol, label)
        started = time.monotonic()
        download_seconds = 0.0
        processing_seconds = 0.0
        try:
            with open_fetched_archive(fetch, url) as content:
                download_seconds = time.monotonic() - started
                _notify(
                    on_progress, "downloaded", current, total, symbol, label,
                    downloaded_bytes=_archive_size(content),
                    elapsed_seconds=download_seconds,
                )
                _notify(on_progress, "processing", current, total, symbol, label)
                processing_started = time.monotonic()
                snapshots = parse_metrics_archive(content, symbol, partition_day)
                if storage_check is not None:
                    storage_check()
                rows = archive.upsert(snapshots)
                processing_seconds = time.monotonic() - processing_started
        except ArchiveNotFoundError:
            _notify(
                on_progress, "unavailable", current, total, symbol, label,
                elapsed_seconds=time.monotonic() - task_started,
            )
            return DownloadResult(symbol, "metrics", label, 0, unavailable=True)
        _notify(
            on_progress, "stored", current, total, symbol, label,
            elapsed_seconds=time.monotonic() - task_started,
            download_seconds=download_seconds, processing_seconds=processing_seconds,
            rows=rows,
        )
        return DownloadResult(symbol, "metrics", label, rows)

    def process(job: tuple[int, tuple[str, date]]) -> DownloadResult:
        current, (symbol, partition_day) = job
        started = time.monotonic()
        try:
            return run_process(job, started)
        except Exception as error:
            _notify(
                on_progress, "failed", current, total, symbol, partition_day.isoformat(),
                elapsed_seconds=time.monotonic() - started,
                error=f"{type(error).__name__}: {error}",
            )
            raise

    indexed_jobs = tuple(enumerate(jobs, start=1))
    if max_workers == 1:
        previous_worker_id = _worker_id()
        _WORKER_CONTEXT.worker_id = 1
        try:
            return [process(job) for job in indexed_jobs]
        finally:
            if indexed_jobs and on_worker_exit is not None:
                on_worker_exit(1)
            _WORKER_CONTEXT.worker_id = previous_worker_id

    worker_ids = iter(range(1, max_workers + 1))
    worker_id_lock = Lock()
    started_worker_ids: set[int] = set()

    def initialize_worker() -> None:
        with worker_id_lock:
            worker_id = next(worker_ids)
            started_worker_ids.add(worker_id)
            _WORKER_CONTEXT.worker_id = worker_id

    try:
        with ThreadPoolExecutor(
            max_workers=max_workers,
            initializer=initialize_worker,
            thread_name_prefix="metrics-archive-worker",
        ) as executor:
            return list(executor.map(process, indexed_jobs))
    finally:
        if on_worker_exit is not None:
            for worker_id in sorted(started_worker_ids):
                on_worker_exit(worker_id)


def build_metrics_index(root: str | Path, *, workers: int = 1) -> Path:
    """Build an atomic sidecar index from metrics Parquet footers."""

    archive_root = Path(root).resolve()
    with _metrics_publish_lock(archive_root, exclusive=True):
        return _build_metrics_index(archive_root, workers=workers)


def publish_metrics_archive(
    root: str | Path, catalog_path: str | Path, *, workers: int = 1
) -> tuple[Path, Path]:
    """Atomically publish a mutually consistent metrics index and catalog."""

    archive_root = Path(root).resolve()
    with _metrics_publish_lock(archive_root, exclusive=True):
        index = _build_metrics_index(archive_root, workers=workers)
        catalog = _create_metrics_catalog(archive_root, catalog_path)
    return index, catalog


def _build_metrics_index(archive_root: Path, *, workers: int) -> Path:
    """Build the metrics index while the caller owns the publication lock."""

    if workers <= 0:
        raise ValueError("index workers must be positive")
    archive_root.mkdir(parents=True, exist_ok=True)
    generation = uuid4().hex
    schema = METRICS_INDEX_SCHEMA.with_metadata({
        b"metrics_index_schema_version": str(METRICS_INDEX_SCHEMA_VERSION).encode(),
        b"metrics_index_generation": generation.encode(),
        b"metrics_root": str(archive_root).encode(),
    })
    index_path = archive_root / METRICS_INDEX_FILENAME
    meta_path = archive_root / METRICS_INDEX_META_FILENAME
    temporary_index = archive_root / f".{METRICS_INDEX_FILENAME}.{generation}.tmp"
    temporary_meta = archive_root / f".{METRICS_INDEX_META_FILENAME}.{generation}.tmp"
    try:
        paths = sorted(archive_root.glob("usdm/*/*/*/*/metrics.parquet"))
        if workers == 1 or len(paths) < 2:
            records = [_inspect_metrics_partition(archive_root, path) for path in paths]
        else:
            with ThreadPoolExecutor(max_workers=min(workers, len(paths))) as executor:
                records = list(executor.map(
                    lambda path: _inspect_metrics_partition(archive_root, path),
                    paths,
                ))
        with pq.ParquetWriter(temporary_index, schema, compression="zstd") as writer:
            if records:
                writer.write_table(pa.Table.from_pylist(records, schema=schema))
        temporary_meta.write_text(json.dumps({
            "schema_version": METRICS_INDEX_SCHEMA_VERSION,
            "generation": generation,
            "completed": True,
            "partition_count": len(records),
        }, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary_index, index_path)
        os.replace(temporary_meta, meta_path)
    finally:
        temporary_index.unlink(missing_ok=True)
        temporary_meta.unlink(missing_ok=True)
    return index_path


def _inspect_metrics_partition(root: Path, path: Path) -> dict[str, object]:
    relative = path.relative_to(root)
    parts = relative.parts
    if len(parts) != 6 or parts[0] != "usdm" or parts[-1] != "metrics.parquet":
        raise MetricsArchiveIndexError(f"invalid metrics partition path: {relative}")
    market, symbol, year, month, day, _ = parts
    parquet = pq.ParquetFile(path)
    if parquet.metadata.num_rows <= 0:
        raise MetricsArchiveIndexError(f"empty metrics partition: {relative}")
    first_snapshot_ms = _footer_timestamp_ms(parquet, "snapshot_time", minimum=True)
    last_snapshot_ms = _footer_timestamp_ms(parquet, "snapshot_time", minimum=False)
    stat = path.stat()
    return {
        "market": market,
        "dataset": "metrics",
        "symbol": symbol,
        "period": METRICS_PERIOD,
        "year": int(year),
        "month": int(month),
        "day": int(day),
        "relative_path": relative.as_posix(),
        "row_count": int(parquet.metadata.num_rows),
        "first_snapshot_ms": first_snapshot_ms,
        "last_snapshot_ms": last_snapshot_ms,
        "file_size": int(stat.st_size),
        "file_mtime_ns": int(stat.st_mtime_ns),
    }


def _footer_timestamp_ms(
    parquet: pq.ParquetFile, column: str, *, minimum: bool
) -> int:
    column_index = parquet.schema_arrow.get_field_index(column)
    if column_index < 0:
        raise MetricsArchiveIndexError(f"metrics partition is missing {column}")
    values = []
    for index in range(parquet.metadata.num_row_groups):
        statistics = parquet.metadata.row_group(index).column(column_index).statistics
        if statistics is None or not statistics.has_min_max:
            raise MetricsArchiveIndexError(
                f"metrics partition has no {column} footer statistics"
            )
        values.append(statistics.min if minimum else statistics.max)
    if not values:
        raise MetricsArchiveIndexError("metrics partition has no row groups")
    value = min(values) if minimum else max(values)
    if hasattr(value, "as_py"):
        value = value.as_py()
    if isinstance(value, datetime):
        return int(value.timestamp() * 1000)
    return int(value)


def load_metrics_index(
    root_or_index: str | Path, *, verify_files: bool = False
) -> pa.Table:
    index_path = Path(root_or_index).resolve()
    if index_path.is_dir():
        index_path /= METRICS_INDEX_FILENAME
    meta_path = index_path.with_name(METRICS_INDEX_META_FILENAME)
    if not index_path.is_file() or not meta_path.is_file():
        raise MetricsArchiveIndexError(
            "metrics index is missing; run market-archive first"
        )
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        parquet = pq.ParquetFile(index_path)
        metadata = parquet.schema_arrow.metadata or {}
        generation = metadata[b"metrics_index_generation"].decode()
        schema_version = int(metadata[b"metrics_index_schema_version"].decode())
        root = Path(metadata[b"metrics_root"].decode())
    except (KeyError, ValueError, json.JSONDecodeError) as error:
        raise MetricsArchiveIndexError("metrics index metadata is invalid") from error
    if (
        not meta.get("completed")
        or meta.get("generation") != generation
        or meta.get("schema_version") != schema_version
        or schema_version != METRICS_INDEX_SCHEMA_VERSION
    ):
        raise MetricsArchiveIndexError("metrics index is incomplete or incompatible")
    table = parquet.read()
    if verify_files:
        for row in table.to_pylist():
            path = root / str(row["relative_path"])
            try:
                stat = path.stat()
            except FileNotFoundError as error:
                raise MetricsArchiveIndexError(
                    f"metrics index is stale: missing {row['relative_path']}"
                ) from error
            if stat.st_size != row["file_size"] or stat.st_mtime_ns != row["file_mtime_ns"]:
                raise MetricsArchiveIndexError(
                    f"metrics index is stale: changed {row['relative_path']}"
                )
    return table


def create_metrics_catalog(root: str | Path, catalog_path: str | Path) -> Path:
    """Create a metrics catalog while excluding concurrent partition writes."""

    archive_root = Path(root).resolve()
    with _metrics_publish_lock(archive_root, exclusive=True):
        return _create_metrics_catalog(archive_root, catalog_path)


def _create_metrics_catalog(archive_root: Path, catalog_path: str | Path) -> Path:
    catalog = Path(catalog_path).resolve()
    catalog.parent.mkdir(parents=True, exist_ok=True)
    temporary = catalog.with_name(f".{catalog.name}.{uuid4().hex}.tmp")
    has_files = next(
        archive_root.glob("usdm/*/*/*/*/metrics.parquet"), None
    ) is not None
    glob = _sql_literal(
        str(archive_root / "usdm" / "*" / "*" / "*" / "*" / "metrics.parquet")
    )
    connection = duckdb.connect(str(temporary))
    try:
        connection.execute("SET TimeZone = 'UTC'")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS metrics_catalog_metadata "
            "(key VARCHAR PRIMARY KEY, value VARCHAR NOT NULL)"
        )
        connection.execute(
            "INSERT OR REPLACE INTO metrics_catalog_metadata VALUES "
            "('metrics_root', ?), ('metrics_index', ?)",
            [str(archive_root), str(archive_root / METRICS_INDEX_FILENAME)],
        )
        if has_files:
            connection.execute(
                f"""
                CREATE OR REPLACE VIEW metrics AS
                SELECT
                    symbol::VARCHAR AS symbol,
                    period::VARCHAR AS period,
                    snapshot_time::TIMESTAMPTZ AS snapshot_time,
                    available_time::TIMESTAMPTZ AS available_time,
                    sum_open_interest::DOUBLE AS sum_open_interest,
                    sum_open_interest_value::DOUBLE AS sum_open_interest_value,
                    count_toptrader_long_short_ratio::DOUBLE
                        AS count_toptrader_long_short_ratio,
                    sum_toptrader_long_short_ratio::DOUBLE
                        AS sum_toptrader_long_short_ratio,
                    count_long_short_ratio::DOUBLE AS count_long_short_ratio,
                    sum_taker_long_short_vol_ratio::DOUBLE
                        AS sum_taker_long_short_vol_ratio,
                    source::VARCHAR AS source,
                    quality_status::VARCHAR AS quality_status,
                    schema_version::SMALLINT AS schema_version
                FROM read_parquet({glob}, union_by_name = true)
                """
            )
        else:
            connection.execute(
                """
                CREATE OR REPLACE VIEW metrics AS
                SELECT
                    NULL::VARCHAR AS symbol,
                    NULL::VARCHAR AS period,
                    NULL::TIMESTAMPTZ AS snapshot_time,
                    NULL::TIMESTAMPTZ AS available_time,
                    NULL::DOUBLE AS sum_open_interest,
                    NULL::DOUBLE AS sum_open_interest_value,
                    NULL::DOUBLE AS count_toptrader_long_short_ratio,
                    NULL::DOUBLE AS sum_toptrader_long_short_ratio,
                    NULL::DOUBLE AS count_long_short_ratio,
                    NULL::DOUBLE AS sum_taker_long_short_vol_ratio,
                    NULL::VARCHAR AS source,
                    NULL::VARCHAR AS quality_status,
                    NULL::SMALLINT AS schema_version
                WHERE false
                """
            )
    finally:
        connection.close()
    try:
        os.replace(temporary, catalog)
    finally:
        temporary.unlink(missing_ok=True)
    return catalog


@contextmanager
def _metrics_publish_lock(root: Path, *, exclusive: bool):
    root.mkdir(parents=True, exist_ok=True)
    lock_file = (root / ".publish.lock").open("a+")
    try:
        mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        fcntl.flock(lock_file.fileno(), mode)
        yield
    finally:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("download range must include a timezone")
    return value.astimezone(UTC)


def _days(start: datetime, end: datetime) -> tuple[date, ...]:
    current = start.date()
    final = (end - timedelta(microseconds=1)).date()
    values: list[date] = []
    while current <= final:
        values.append(current)
        current += timedelta(days=1)
    return tuple(values)


def _parse_partition_day(value: date | str) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"invalid metrics partition date: {value!r}") from error


def _archive_size(content: bytes | io.BufferedIOBase) -> int:
    if isinstance(content, bytes):
        return len(content)
    position = content.tell()
    content.seek(0, io.SEEK_END)
    size = content.tell()
    content.seek(position)
    return size


def _notify(
    callback: Callable[[DownloadProgress], None] | None,
    phase: str,
    current: int,
    total: int,
    symbol: str,
    period: str,
    *,
    downloaded_bytes: int = 0,
    elapsed_seconds: float = 0.0,
    rows: int = 0,
    error: str = "",
    download_seconds: float = 0.0,
    processing_seconds: float = 0.0,
) -> None:
    if callback is None:
        return
    callback(DownloadProgress(
        phase=phase,
        current=current,
        total=total,
        symbol=symbol,
        timeframe="metrics",
        period=period,
        worker_id=_worker_id(),
        downloaded_bytes=downloaded_bytes,
        elapsed_seconds=elapsed_seconds,
        rows=rows,
        error=error,
        download_seconds=download_seconds,
        processing_seconds=processing_seconds,
    ))


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _worker_id() -> int:
    return int(getattr(_WORKER_CONTEXT, "worker_id", current_archive_worker_id()))
