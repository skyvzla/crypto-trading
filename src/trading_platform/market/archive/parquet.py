from __future__ import annotations

import fcntl
import os
from collections.abc import Iterable, Sequence
from pathlib import Path
from threading import Lock
from uuid import uuid4

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from .models import Candle, Candle1s


ARCHIVE_CATALOG_SCHEMA_VERSION = 2


class ArchivePartitionConflictError(RuntimeError):
    pass


CANDLE_BASE_COLUMNS = (
    "symbol", "timeframe", "open_time", "open", "high", "low", "close",
    "volume", "close_time",
)
CANDLE_BASE_SCHEMA = pa.schema(
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
CANDLE_FEATURE_COLUMNS = (
    "vwap",
    "quote_volume",
    "trade_count",
    "raw_trade_count",
    "taker_buy_volume",
    "taker_sell_volume",
    "taker_buy_quote_volume",
    "taker_sell_quote_volume",
    "taker_buy_trade_count",
    "taker_sell_trade_count",
    "taker_buy_agg_trade_count",
    "taker_sell_agg_trade_count",
    "max_agg_trade_quantity",
    "max_taker_buy_agg_trade_quantity",
    "max_taker_sell_agg_trade_quantity",
    "first_aggregate_trade_id",
    "last_aggregate_trade_id",
    "first_trade_id",
    "last_trade_id",
)
CANDLE_SCHEMA = pa.schema(
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
        ("vwap", pa.float64()),
        ("quote_volume", pa.float64()),
        ("trade_count", pa.int64()),
        ("raw_trade_count", pa.int64()),
        ("taker_buy_volume", pa.float64()),
        ("taker_sell_volume", pa.float64()),
        ("taker_buy_quote_volume", pa.float64()),
        ("taker_sell_quote_volume", pa.float64()),
        ("taker_buy_trade_count", pa.int64()),
        ("taker_sell_trade_count", pa.int64()),
        ("taker_buy_agg_trade_count", pa.int64()),
        ("taker_sell_agg_trade_count", pa.int64()),
        ("max_agg_trade_quantity", pa.float64()),
        ("max_taker_buy_agg_trade_quantity", pa.float64()),
        ("max_taker_sell_agg_trade_quantity", pa.float64()),
        ("first_aggregate_trade_id", pa.int64()),
        ("last_aggregate_trade_id", pa.int64()),
        ("first_trade_id", pa.int64()),
        ("last_trade_id", pa.int64()),
    ]
)
_DUCKDB_CANDLE_TYPES = {
    "symbol": "VARCHAR",
    "timeframe": "VARCHAR",
    "open_time": "TIMESTAMPTZ",
    "open": "DOUBLE",
    "high": "DOUBLE",
    "low": "DOUBLE",
    "close": "DOUBLE",
    "volume": "DOUBLE",
    "close_time": "TIMESTAMPTZ",
    "vwap": "DOUBLE",
    "quote_volume": "DOUBLE",
    "trade_count": "BIGINT",
    "raw_trade_count": "BIGINT",
    "taker_buy_volume": "DOUBLE",
    "taker_sell_volume": "DOUBLE",
    "taker_buy_quote_volume": "DOUBLE",
    "taker_sell_quote_volume": "DOUBLE",
    "taker_buy_trade_count": "BIGINT",
    "taker_sell_trade_count": "BIGINT",
    "taker_buy_agg_trade_count": "BIGINT",
    "taker_sell_agg_trade_count": "BIGINT",
    "max_agg_trade_quantity": "DOUBLE",
    "max_taker_buy_agg_trade_quantity": "DOUBLE",
    "max_taker_sell_agg_trade_quantity": "DOUBLE",
    "first_aggregate_trade_id": "BIGINT",
    "last_aggregate_trade_id": "BIGINT",
    "first_trade_id": "BIGINT",
    "last_trade_id": "BIGINT",
}


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
        self._retired_paths: set[Path] = set()
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
                _remove_partition_files(sorted(self._retired_paths))
                build_archive_index(self.root, workers=self.index_workers)
            else:
                update_archive_index(
                    self.root,
                    self._changed_paths,
                    removed_paths=self._retired_paths,
                )
                _remove_partition_files(sorted(self._retired_paths))

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
            return self._indexed_rows.get(normalized_key)
        target = self._partition_dir(symbol, timeframe, year, month, day)
        target /= "candles.parquet"
        if not target.is_file():
            return None
        return pq.ParquetFile(target).metadata.num_rows

    def upsert(
        self, candles: Iterable[Candle], *, partition_day: int | None = None
    ) -> int:
        rows = list(candles)
        if not rows:
            return 0
        keys = {
            (
                candle.normalized_symbol,
                candle.timeframe,
                candle.open_time_utc.year,
                candle.open_time_utc.month,
                (
                    partition_day
                    if partition_day is not None
                    else candle.open_time_utc.day
                    if candle.timeframe == "1s"
                    else 0
                ),
            )
            for candle in rows
        }
        if len(keys) != 1:
            raise ValueError(
                "one Parquet write must contain exactly one archive partition"
            )
        symbol, timeframe, year, month, day = keys.pop()
        columns = {
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
        if timeframe == "1s":
            feature_rows = [item if isinstance(item, Candle1s) else None for item in rows]
            feature_types = {field.name: field.type for field in CANDLE_SCHEMA}
            for name in CANDLE_FEATURE_COLUMNS:
                columns[name] = pa.array(
                    [getattr(item, name, None) if item is not None else None for item in feature_rows],
                    type=feature_types[name],
                )
        table = pa.table(columns)
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
        unknown = set(table.column_names) - set(CANDLE_SCHEMA.names)
        missing_base = set(CANDLE_BASE_COLUMNS) - set(table.column_names)
        if unknown or missing_base:
            raise ValueError("candle Arrow table has incompatible columns")
        normalized_symbol = symbol.strip().upper()
        normalized_timeframe = timeframe.strip().lower()
        if normalized_timeframe == "1s" and day == 0:
            raise ArchivePartitionConflictError(
                "1s archive must use daily partitions"
            )
        if normalized_timeframe == "1s":
            for field in CANDLE_SCHEMA:
                if field.name not in table.column_names:
                    table = table.append_column(
                        field.name, pa.nulls(table.num_rows, type=field.type)
                    )
            table = table.select(CANDLE_SCHEMA.names).cast(CANDLE_SCHEMA)
        else:
            # 订单流扩展只属于 aggTrade 生成的 1s 数据；其它 K 线继续写窄表。
            table = table.select(CANDLE_BASE_SCHEMA.names).cast(CANDLE_BASE_SCHEMA)
        month_partition = self._partition_dir(
            normalized_symbol, normalized_timeframe, year, month, 0
        ).parent
        month_partition.mkdir(parents=True, exist_ok=True)
        month_lock_file = (month_partition / ".partition.lock").open("a+")
        fcntl.flock(
            month_lock_file.fileno(),
            fcntl.LOCK_EX if day == 0 else fcntl.LOCK_SH,
        )
        try:
            monthly_target = month_partition / "00" / "candles.parquet"
            if day > 0 and monthly_target.is_file():
                raise ArchivePartitionConflictError(
                    "cannot write a daily partition while a monthly partition exists: "
                    f"{monthly_target}"
                )
            daily_targets = sorted(
                path
                for path in month_partition.glob("*/candles.parquet")
                if path.parent.name != "00"
            )
            if day == 0 and daily_targets:
                _require_open_time_coverage(
                    required_paths=daily_targets,
                    covering_table=table,
                    context=(
                        f"monthly replacement for {normalized_symbol} "
                        f"{normalized_timeframe} {year:04d}-{month:02d}"
                    ),
                )

            partition = self._partition_dir(
                normalized_symbol, normalized_timeframe, year, month, day
            )
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
                retired_paths = daily_targets if day == 0 else []
                with self._state_lock:
                    self._dirty = True
                    self._changed_paths.add(target)
                    self._retired_paths.update(retired_paths)
                    if self._indexed_rows is not None:
                        self._indexed_rows[
                            (
                                normalized_symbol,
                                normalized_timeframe,
                                year,
                                month,
                                day,
                            )
                        ] = table.num_rows
                        for retired in retired_paths:
                            removed_day = int(retired.parent.name)
                            self._indexed_rows.pop(
                                (
                                    normalized_symbol,
                                    normalized_timeframe,
                                    year,
                                    month,
                                    removed_day,
                                ),
                                None,
                            )
            finally:
                temporary.unlink(missing_ok=True)
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                lock_file.close()
        finally:
            fcntl.flock(month_lock_file.fileno(), fcntl.LOCK_UN)
            month_lock_file.close()
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


def repair_mixed_candle_partitions(root: str | Path) -> list[Path]:
    """Remove inactive side of a mixed monthly/daily layout after coverage checks."""

    archive_root = Path(root).resolve()
    indexed_layouts = _indexed_partition_layouts(archive_root)
    candidates: list[tuple[Path, list[Path], list[Path]]] = []
    for monthly_target in sorted(archive_root.glob("*/*/*/*/00/candles.parquet")):
        month_partition = monthly_target.parent.parent
        daily_targets = sorted(
            path
            for path in month_partition.glob("*/candles.parquet")
            if path.parent.name != "00"
        )
        if not daily_targets:
            continue
        relative = monthly_target.relative_to(archive_root)
        symbol, timeframe, year, month = relative.parts[:4]
        indexed_days = indexed_layouts.get(
            (symbol.upper(), timeframe.lower(), int(year), int(month)), set()
        )
        if 0 in indexed_days and not any(day > 0 for day in indexed_days):
            required_paths = daily_targets
            covering_paths = [monthly_target]
            inactive_paths = daily_targets
        else:
            required_paths = [monthly_target]
            covering_paths = daily_targets
            inactive_paths = [monthly_target]
        _require_open_time_coverage(
            required_paths=required_paths,
            covering_paths=covering_paths,
            context=f"mixed archive layout under {month_partition}",
        )
        candidates.append((month_partition, covering_paths, inactive_paths))

    removed: list[Path] = []
    for month_partition, covering_paths, inactive_paths in candidates:
        month_lock_file = (month_partition / ".partition.lock").open("a+")
        fcntl.flock(month_lock_file.fileno(), fcntl.LOCK_EX)
        try:
            _require_open_time_coverage(
                required_paths=inactive_paths,
                covering_paths=covering_paths,
                context=f"mixed archive layout under {month_partition}",
            )
            removed.extend(_remove_partition_files(inactive_paths))
        finally:
            fcntl.flock(month_lock_file.fileno(), fcntl.LOCK_UN)
            month_lock_file.close()
    return removed


def _indexed_partition_layouts(
    archive_root: Path,
) -> dict[tuple[str, str, int, int], set[int]]:
    index_path = archive_root / "archive_index.parquet"
    if not index_path.is_file():
        return {}
    table = pq.read_table(
        index_path,
        columns=["symbol", "timeframe", "year", "month", "day"],
    )
    layouts: dict[tuple[str, str, int, int], set[int]] = {}
    for batch in table.to_batches(max_chunksize=4096):
        for row in batch.to_pylist():
            key = (
                str(row["symbol"]).upper(),
                str(row["timeframe"]).lower(),
                int(row["year"]),
                int(row["month"]),
            )
            layouts.setdefault(key, set()).add(int(row["day"]))
    return layouts


def _require_open_time_coverage(
    *,
    required_paths: Sequence[Path],
    context: str,
    covering_paths: Sequence[Path] = (),
    covering_table: pa.Table | None = None,
) -> None:
    required = _open_times_from_paths(required_paths)
    if covering_table is None:
        covering = _open_times_from_paths(covering_paths)
    else:
        covering_rows = covering_table["open_time"].to_pylist()
        covering = set(covering_rows)
        if len(covering_rows) != len(covering):
            raise ArchivePartitionConflictError(
                f"{context} contains duplicate open_time values"
            )
    missing = required - covering
    if missing:
        raise ArchivePartitionConflictError(
            f"{context} is not fully covered; {len(missing)} timestamps are missing"
        )


def _open_times_from_paths(paths: Sequence[Path]) -> set[object]:
    values: set[object] = set()
    for path in paths:
        column = pq.read_table(path, columns=["open_time"])["open_time"]
        rows = column.to_pylist()
        if len(rows) != len(set(rows)):
            raise ArchivePartitionConflictError(
                f"partition contains duplicate open_time values: {path}"
            )
        overlap = values.intersection(rows)
        if overlap:
            raise ArchivePartitionConflictError(
                f"daily partitions overlap at {path}"
            )
        values.update(rows)
    return values


def _remove_partition_files(paths: Sequence[Path]) -> list[Path]:
    removed: list[Path] = []
    for path in paths:
        if not path.is_file():
            continue
        path.unlink()
        removed.append(path)
        path.with_name(".write.lock").unlink(missing_ok=True)
        try:
            path.parent.rmdir()
        except OSError:
            pass
    return removed


def create_duckdb_catalog(root: str | Path, catalog_path: str | Path) -> Path:
    dataset = Path(root).resolve()
    index_path = dataset / "archive_index.parquet"
    physical_files_exist = (
        next(dataset.glob("*/*/*/*/*/candles.parquet"), None) is not None
    )
    if index_path.is_file():
        from .index import load_archive_index

        has_files = not load_archive_index(index_path).empty
    elif physical_files_exist:
        raise RuntimeError(
            "archive index is required before creating a DuckDB catalog"
        )
    else:
        has_files = False
    catalog = Path(catalog_path).resolve()
    catalog.parent.mkdir(parents=True, exist_ok=True)
    glob = _sql_literal(
        str(dataset / "*" / "*" / "*" / "*" / "*" / "candles.parquet")
    )
    index_literal = _sql_literal(str(index_path))
    root_prefix = _sql_literal(str(dataset) + "/")
    connection = duckdb.connect(str(catalog))
    try:
        connection.execute("SET TimeZone = 'UTC'")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS archive_catalog_metadata "
            "(key VARCHAR PRIMARY KEY, value VARCHAR NOT NULL)"
        )
        connection.execute(
            "INSERT OR REPLACE INTO archive_catalog_metadata VALUES "
            "('archive_root', ?), ('archive_index', ?), "
            "('catalog_schema_version', ?)",
            [
                str(dataset),
                str(index_path),
                str(ARCHIVE_CATALOG_SCHEMA_VERSION),
            ],
        )
        source_columns: set[str] = set()
        if has_files:
            source_columns = {
                str(row[0])
                for row in connection.execute(
                    "DESCRIBE SELECT * FROM "
                    f"read_parquet({glob}, union_by_name=true, filename=true)"
                ).fetchall()
            }
        select_columns = []
        for name, data_type in _DUCKDB_CANDLE_TYPES.items():
            if name in source_columns:
                select_columns.append(f'source.{name}::{data_type} AS {name}')
            else:
                select_columns.append(f'NULL::{data_type} AS {name}')
        source = (
            "FROM "
            f"read_parquet({glob}, union_by_name=true, filename=true) AS source"
            if has_files
            else ""
        )
        where = (
            " WHERE source.filename IN ("
            f"SELECT {root_prefix} || relative_path FROM read_parquet({index_literal})"
            ")"
            if has_files
            else " WHERE false"
        )
        connection.execute(
            "CREATE OR REPLACE VIEW candles AS SELECT "
            + ", ".join(select_columns)
            + " "
            + source
            + where
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
        columns = {
            str(item[0])
            for item in connection.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'main' AND table_name = 'candles'"
            ).fetchall()
        }
        version_row = connection.execute(
            "SELECT value FROM archive_catalog_metadata "
            "WHERE key = 'catalog_schema_version'"
        ).fetchone()
    finally:
        connection.close()
    has_parquet_view = row is not None and "read_parquet" in str(row[0]).lower()
    if (
        has_indexed_partitions != has_parquet_view
        or not set(_DUCKDB_CANDLE_TYPES).issubset(columns)
        or version_row != (str(ARCHIVE_CATALOG_SCHEMA_VERSION),)
    ):
        return create_duckdb_catalog(dataset, catalog)
    return catalog


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"
