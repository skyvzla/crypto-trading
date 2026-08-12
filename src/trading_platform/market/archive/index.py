from __future__ import annotations

import fcntl
import json
import multiprocessing
import os
from collections.abc import Iterator
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime
from itertools import islice, repeat
from pathlib import Path
from typing import Any
from uuid import uuid4

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

ARCHIVE_INDEX_FILENAME = "archive_index.parquet"
ARCHIVE_INDEX_META_FILENAME = "archive_index.meta.json"
ARCHIVE_INDEX_SCHEMA_VERSION = 1
INDEX_SCAN_BATCH_SIZE = 4096
INDEX_WRITE_BATCH_SIZE = 4096


class ArchiveIndexError(RuntimeError):
    pass


INDEX_SCHEMA = pa.schema([
    ("symbol", pa.string()),
    ("timeframe", pa.string()),
    ("year", pa.int16()),
    ("month", pa.int8()),
    ("day", pa.int8()),
    ("relative_path", pa.string()),
    ("row_count", pa.int64()),
    ("first_open_ms", pa.int64()),
    ("last_close_ms", pa.int64()),
    ("file_size", pa.int64()),
    ("file_mtime_ns", pa.int64()),
])


def _timestamp_ms(value: Any) -> int:
    if isinstance(value, datetime):
        return int(value.timestamp() * 1000)
    if hasattr(value, "as_py"):
        return _timestamp_ms(value.as_py())
    return int(value)


def _column_stat(parquet: pq.ParquetFile, column: str, *, minimum: bool) -> int:
    column_index = parquet.schema_arrow.get_field_index(column)
    values = []
    for index in range(parquet.metadata.num_row_groups):
        statistics = parquet.metadata.row_group(index).column(column_index).statistics
        if statistics is None or not statistics.has_min_max:
            raise ArchiveIndexError(
                f"Parquet partition has no {column} footer statistics"
            )
        values.append(statistics.min if minimum else statistics.max)
    if not values:
        raise ArchiveIndexError("Parquet partition has no row groups")
    selected = min(values) if minimum else max(values)
    return _timestamp_ms(selected)


def _inspect_partition(root_text: str, path_text: str) -> dict[str, Any]:
    root = Path(root_text)
    path = Path(path_text)
    relative = path.relative_to(root)
    parts = relative.parts
    if len(parts) != 6 or parts[-1] != "candles.parquet":
        raise ArchiveIndexError(f"invalid archive partition path: {relative}")
    symbol, timeframe, year, month, day, _filename = parts
    parquet = pq.ParquetFile(path)
    row_count = parquet.metadata.num_rows
    if row_count <= 0:
        raise ArchiveIndexError(f"empty archive partition: {relative}")
    stat = path.stat()
    return {
        "symbol": symbol.strip().upper(),
        "timeframe": timeframe.strip().lower(),
        "year": int(year),
        "month": int(month),
        "day": int(day),
        "relative_path": relative.as_posix(),
        "row_count": int(row_count),
        "first_open_ms": _column_stat(parquet, "open_time", minimum=True),
        "last_close_ms": _column_stat(parquet, "close_time", minimum=False),
        "file_size": int(stat.st_size),
        "file_mtime_ns": int(stat.st_mtime_ns),
    }


def _write_index_rows(
    writer: pq.ParquetWriter,
    rows: Iterator[dict[str, Any]],
    *,
    schema: pa.Schema,
) -> int:
    row_count = 0
    while batch := list(islice(rows, INDEX_WRITE_BATCH_SIZE)):
        writer.write_table(pa.Table.from_pylist(batch, schema=schema))
        row_count += len(batch)
    return row_count


def build_archive_index(
    root: str | Path,
    *,
    workers: int | None = None,
) -> Path:
    archive_root = Path(root).resolve()
    archive_root.mkdir(parents=True, exist_ok=True)
    worker_count = workers if workers is not None else min(8, os.cpu_count() or 1)
    if worker_count <= 0:
        raise ValueError("index workers must be positive")
    with _index_lock(archive_root):
        return _build_archive_index(archive_root, worker_count)


def _build_archive_index(archive_root: Path, worker_count: int) -> Path:
    root_text = str(archive_root)
    generation = uuid4().hex
    metadata = {
        b"archive_index_schema_version": str(ARCHIVE_INDEX_SCHEMA_VERSION).encode(),
        b"archive_index_generation": generation.encode(),
        b"archive_root": str(archive_root).encode(),
    }
    schema = INDEX_SCHEMA.with_metadata(metadata)
    index_path = archive_root / ARCHIVE_INDEX_FILENAME
    meta_path = archive_root / ARCHIVE_INDEX_META_FILENAME
    temporary_index = archive_root / f".{ARCHIVE_INDEX_FILENAME}.{generation}.tmp"
    temporary_meta = archive_root / f".{ARCHIVE_INDEX_META_FILENAME}.{generation}.tmp"
    try:
        partition_count = 0
        paths = (
            str(path)
            for path in archive_root.glob("*/*/*/*/*/candles.parquet")
        )
        with pq.ParquetWriter(temporary_index, schema, compression="zstd") as writer:
            if worker_count > 1:
                path_batch = list(islice(paths, INDEX_SCAN_BATCH_SIZE))
                if path_batch:
                    with ProcessPoolExecutor(
                        max_workers=min(worker_count, len(path_batch)),
                        mp_context=multiprocessing.get_context("spawn"),
                    ) as pool:
                        while path_batch:
                            inspected = pool.map(
                                _inspect_partition,
                                repeat(root_text),
                                path_batch,
                                chunksize=32,
                            )
                            partition_count += _write_index_rows(
                                writer, inspected, schema=schema
                            )
                            path_batch = list(
                                islice(paths, INDEX_SCAN_BATCH_SIZE)
                            )
            else:
                inspected = (_inspect_partition(root_text, path) for path in paths)
                partition_count = _write_index_rows(
                    writer, inspected, schema=schema
                )
        temporary_meta.write_text(json.dumps({
            "schema_version": ARCHIVE_INDEX_SCHEMA_VERSION,
            "generation": generation,
            "completed": True,
            "partition_count": partition_count,
        }, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary_index, index_path)
        os.replace(temporary_meta, meta_path)
    finally:
        temporary_index.unlink(missing_ok=True)
        temporary_meta.unlink(missing_ok=True)
    return index_path


def update_archive_index(
    root: str | Path,
    changed_paths: Iterator[Path] | list[Path] | set[Path] | tuple[Path, ...],
) -> Path:
    """Merge changed partitions without scanning unchanged archive files."""

    archive_root = Path(root).resolve()
    paths = tuple(Path(path).resolve() for path in changed_paths)
    with _index_lock(archive_root):
        index_path = archive_root / ARCHIVE_INDEX_FILENAME
        records: dict[str, dict[str, Any]] = {}
        if index_path.is_file():
            frame = load_archive_index(index_path)
            records = {
                str(row["relative_path"]): row
                for row in frame.to_dict(orient="records")
            }
        for path in paths:
            record = _inspect_partition(str(archive_root), str(path))
            records[str(record["relative_path"])] = record
        return _write_archive_index_records(
            archive_root,
            (records[key] for key in sorted(records)),
        )


def _write_archive_index_records(
    archive_root: Path,
    records: Iterator[dict[str, Any]],
) -> Path:
    generation = uuid4().hex
    schema = INDEX_SCHEMA.with_metadata({
        b"archive_index_schema_version": str(ARCHIVE_INDEX_SCHEMA_VERSION).encode(),
        b"archive_index_generation": generation.encode(),
        b"archive_root": str(archive_root).encode(),
    })
    index_path = archive_root / ARCHIVE_INDEX_FILENAME
    meta_path = archive_root / ARCHIVE_INDEX_META_FILENAME
    temporary_index = archive_root / f".{ARCHIVE_INDEX_FILENAME}.{generation}.tmp"
    temporary_meta = archive_root / f".{ARCHIVE_INDEX_META_FILENAME}.{generation}.tmp"
    try:
        with pq.ParquetWriter(temporary_index, schema, compression="zstd") as writer:
            partition_count = _write_index_rows(writer, records, schema=schema)
        temporary_meta.write_text(json.dumps({
            "schema_version": ARCHIVE_INDEX_SCHEMA_VERSION,
            "generation": generation,
            "completed": True,
            "partition_count": partition_count,
        }, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary_index, index_path)
        os.replace(temporary_meta, meta_path)
    finally:
        temporary_index.unlink(missing_ok=True)
        temporary_meta.unlink(missing_ok=True)
    return index_path


class _index_lock:
    def __init__(self, root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True)
        self._file = (root / ".index.lock").open("a+")

    def __enter__(self) -> None:
        fcntl.flock(self._file.fileno(), fcntl.LOCK_EX)

    def __exit__(self, *_exc: object) -> None:
        fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        self._file.close()


def load_archive_index(
    path: str | Path,
    *,
    verify_files: bool = False,
) -> pd.DataFrame:
    index_path = Path(path).resolve()
    if index_path.is_dir():
        index_path /= ARCHIVE_INDEX_FILENAME
    meta_path = index_path.with_name(ARCHIVE_INDEX_META_FILENAME)
    if not index_path.is_file() or not meta_path.is_file():
        raise ArchiveIndexError(
            "archive index is missing; run market-archive-index first"
        )
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        parquet = pq.ParquetFile(index_path)
        schema_metadata = parquet.schema_arrow.metadata or {}
        generation = schema_metadata[b"archive_index_generation"].decode()
        schema_version = int(
            schema_metadata[b"archive_index_schema_version"].decode()
        )
    except (KeyError, ValueError, json.JSONDecodeError) as error:
        raise ArchiveIndexError("archive index metadata is invalid") from error
    if (
        not meta.get("completed")
        or meta.get("generation") != generation
        or meta.get("schema_version") != schema_version
        or schema_version != ARCHIVE_INDEX_SCHEMA_VERSION
    ):
        raise ArchiveIndexError("archive index is incomplete or incompatible")
    frame = parquet.read().to_pandas()
    if verify_files and not frame.empty:
        root_value = schema_metadata.get(b"archive_root")
        if root_value is None:
            raise ArchiveIndexError("archive index root metadata is missing")
        verify_archive_index_files(frame, Path(root_value.decode()))
    return frame


def verify_archive_index_files(frame: pd.DataFrame, root: str | Path) -> None:
    archive_root = Path(root).resolve()
    for row in frame.itertuples(index=False):
        partition = archive_root / row.relative_path
        try:
            stat = partition.stat()
        except FileNotFoundError as error:
            raise ArchiveIndexError(
                f"archive index is stale: missing {row.relative_path}"
            ) from error
        if stat.st_size != row.file_size or stat.st_mtime_ns != row.file_mtime_ns:
            raise ArchiveIndexError(
                f"archive index is stale: changed {row.relative_path}"
            )
