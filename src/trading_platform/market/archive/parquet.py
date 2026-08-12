from __future__ import annotations

import fcntl
import os
from collections.abc import Iterable
from pathlib import Path
from threading import Lock
from uuid import uuid4

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from .models import Candle


class ParquetCandleArchive:
    """Immutable, atomically replaced candle partitions."""

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
        self._state_lock = Lock()
        self._changed_paths: set[Path] = set()
        self._indexed_rows: dict[tuple[str, str, int, int, int], int] | None = None
        index_path = self.root / "archive_index.parquet"
        if index_path.is_file():
            from .index import load_archive_index

            frame = load_archive_index(index_path)
            self._indexed_rows = {
                (
                    str(row.symbol), str(row.timeframe), int(row.year),
                    int(row.month), int(row.day),
                ): int(row.row_count)
                for row in frame.itertuples(index=False)
            }

    def __enter__(self) -> "ParquetCandleArchive":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._dirty and self.rebuild_index_on_close:
            from .index import build_archive_index, update_archive_index

            if self._indexed_rows is None:
                build_archive_index(self.root, workers=self.index_workers)
            else:
                update_archive_index(self.root, self._changed_paths)

    def partition_rows(
        self,
        symbol: str,
        timeframe: str,
        year: int,
        month: int,
        day: int,
    ) -> int | None:
        normalized_key = (
            symbol.strip().upper(), timeframe.strip().lower(), year, month, day
        )
        if self._indexed_rows is not None:
            indexed = self._indexed_rows.get(normalized_key)
            if indexed is not None:
                return indexed
        target = self._partition_dir(symbol, timeframe, year, month, day)
        target /= "candles.parquet"
        if not target.is_file():
            return None
        return pq.ParquetFile(target).metadata.num_rows

    def upsert(self, candles: Iterable[Candle]) -> int:
        rows = list(candles)
        if not rows:
            return 0
        keys = {
            (
                candle.normalized_symbol,
                candle.timeframe,
                candle.open_time_utc.year,
                candle.open_time_utc.month,
                candle.open_time_utc.day if candle.timeframe == "1s" else 0,
            )
            for candle in rows
        }
        if len(keys) != 1:
            raise ValueError(
                "one Parquet write must contain exactly one archive partition"
            )
        symbol, timeframe, year, month, day = keys.pop()
        table = pa.table(
            {
                "symbol": pa.array([symbol] * len(rows), type=pa.string()),
                "timeframe": pa.array([timeframe] * len(rows), type=pa.string()),
                "open_time": pa.array(
                    [item.open_time_utc for item in rows],
                    type=pa.timestamp("ms", tz="UTC"),
                ),
                "open": pa.array([item.open for item in rows], type=pa.float64()),
                "high": pa.array([item.high for item in rows], type=pa.float64()),
                "low": pa.array([item.low for item in rows], type=pa.float64()),
                "close": pa.array([item.close for item in rows], type=pa.float64()),
                "volume": pa.array([item.volume for item in rows], type=pa.float64()),
                "close_time": pa.array(
                    [item.close_time_utc for item in rows],
                    type=pa.timestamp("ms", tz="UTC"),
                ),
            }
        )
        return self.upsert_table(
            table,
            symbol=symbol,
            timeframe=timeframe,
            year=year,
            month=month,
            day=day,
        )

    def upsert_table(
        self,
        table: pa.Table,
        *,
        symbol: str,
        timeframe: str,
        year: int,
        month: int,
        day: int,
    ) -> int:
        """Atomically store one already-columnar archive partition."""

        if not table.num_rows:
            return 0
        schema = pa.schema(
            [
                ("symbol", pa.string()),
                ("timeframe", pa.string()),
                ("open_time", pa.timestamp("ms", tz="UTC")),
                ("open", pa.float64()),
                ("high", pa.float64()),
                ("low", pa.float64()),
                ("close", pa.float64()),
                ("volume", pa.float64()),
                ("close_time", pa.timestamp("ms", tz="UTC")),
            ]
        )
        if set(table.column_names) != set(schema.names):
            raise ValueError("candle Arrow table has incompatible columns")
        table = table.select(schema.names).cast(schema)
        partition = self._partition_dir(symbol, timeframe, year, month, day)
        partition.mkdir(parents=True, exist_ok=True)
        target = partition / "candles.parquet"
        temporary = partition / f".candles-{uuid4().hex}.tmp.parquet"
        lock_file = (partition / ".write.lock").open("a+")
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            lock_file.close()
            raise RuntimeError(
                f"partition writer is already active for {partition}"
            ) from error
        try:
            pq.write_table(table, temporary, compression="zstd")
            os.replace(temporary, target)
            with self._state_lock:
                self._dirty = True
                self._changed_paths.add(target)
                if self._indexed_rows is not None:
                    self._indexed_rows[
                        (symbol.upper(), timeframe.lower(), year, month, day)
                    ] = table.num_rows
        finally:
            temporary.unlink(missing_ok=True)
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            lock_file.close()
        return table.num_rows

    def _partition_dir(
        self,
        symbol: str,
        timeframe: str,
        year: int,
        month: int,
        day: int,
    ) -> Path:
        return (
            self.root
            / symbol.strip().upper()
            / timeframe.strip().lower()
            / f"{year:04d}"
            / f"{month:02d}"
            / f"{day:02d}"
        )


def create_duckdb_catalog(root: str | Path, catalog_path: str | Path) -> Path:
    dataset = Path(root).resolve()
    has_files = next(dataset.glob("*/*/*/*/*/candles.parquet"), None) is not None
    catalog = Path(catalog_path).resolve()
    catalog.parent.mkdir(parents=True, exist_ok=True)
    glob = _sql_literal(
        str(dataset / "*" / "*" / "*" / "*" / "*" / "candles.parquet")
    )
    connection = duckdb.connect(str(catalog))
    try:
        connection.execute("SET TimeZone = 'UTC'")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS archive_catalog_metadata "
            "(key VARCHAR PRIMARY KEY, value VARCHAR NOT NULL)"
        )
        connection.execute(
            "INSERT OR REPLACE INTO archive_catalog_metadata VALUES "
            "('archive_root', ?), ('archive_index', ?)",
            [
                str(dataset),
                str(dataset / "archive_index.parquet"),
            ],
        )
        if has_files:
            connection.execute(
                f"""
            CREATE OR REPLACE VIEW candles AS
            SELECT
                symbol::VARCHAR AS symbol,
                timeframe::VARCHAR AS timeframe,
                open_time::TIMESTAMPTZ AS open_time,
                open::DOUBLE AS open,
                high::DOUBLE AS high,
                low::DOUBLE AS low,
                close::DOUBLE AS close,
                volume::DOUBLE AS volume,
                close_time::TIMESTAMPTZ AS close_time
            FROM read_parquet(
                {glob}, union_by_name = true
            )
            """
            )
        else:
            connection.execute(
                """
                CREATE OR REPLACE VIEW candles AS
                SELECT
                    NULL::VARCHAR AS symbol,
                    NULL::VARCHAR AS timeframe,
                    NULL::TIMESTAMPTZ AS open_time,
                    NULL::DOUBLE AS open,
                    NULL::DOUBLE AS high,
                    NULL::DOUBLE AS low,
                    NULL::DOUBLE AS close,
                    NULL::DOUBLE AS volume,
                    NULL::TIMESTAMPTZ AS close_time
                WHERE false
                """
            )
    finally:
        connection.close()
    return catalog


def archive_root_from_catalog(catalog_path: str | Path) -> Path:
    connection = duckdb.connect(str(catalog_path), read_only=True)
    try:
        row = connection.execute(
            "SELECT value FROM archive_catalog_metadata "
            "WHERE key = 'archive_root'"
        ).fetchone()
    except duckdb.Error as error:
        raise RuntimeError(
            "DuckDB catalog has no archive metadata; rebuild the archive index"
        ) from error
    finally:
        connection.close()
    if row is None:
        raise RuntimeError(
            "DuckDB catalog has no archive root; rebuild the archive index"
        )
    return Path(str(row[0])).resolve()


def ensure_duckdb_catalog(root: str | Path, catalog_path: str | Path) -> Path:
    """Reuse a matching catalog; explicit index rebuilds handle refresh/repair."""

    dataset = Path(root).resolve()
    catalog = Path(catalog_path).resolve()
    if not catalog.is_file():
        return create_duckdb_catalog(dataset, catalog)
    if archive_root_from_catalog(catalog) != dataset:
        raise RuntimeError(
            "DuckDB catalog points to another archive root; "
            "run market-archive-index to rebuild it"
        )
    from .index import load_archive_index

    has_indexed_partitions = not load_archive_index(dataset).empty
    connection = duckdb.connect(str(catalog), read_only=True)
    try:
        row = connection.execute(
            "SELECT sql FROM duckdb_views() WHERE view_name = 'candles'"
        ).fetchone()
    finally:
        connection.close()
    has_parquet_view = row is not None and "read_parquet" in str(row[0]).lower()
    if has_indexed_partitions != has_parquet_view:
        return create_duckdb_catalog(dataset, catalog)
    return catalog


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"
