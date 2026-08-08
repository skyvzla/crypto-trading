from __future__ import annotations

import fcntl
import os
from collections.abc import Iterable
from pathlib import Path
from uuid import uuid4

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from .models import Candle


class ParquetCandleArchive:
    """Immutable, atomically replaced candle partitions."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def __enter__(self) -> "ParquetCandleArchive":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        return None

    def partition_rows(
        self,
        symbol: str,
        timeframe: str,
        year: int,
        month: int,
        day: int,
    ) -> int | None:
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
        partition = self._partition_dir(symbol, timeframe, year, month, day)
        partition.mkdir(parents=True, exist_ok=True)
        target = partition / "candles.parquet"
        temporary = partition / f".candles-{uuid4().hex}.tmp.parquet"
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
        finally:
            temporary.unlink(missing_ok=True)
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            lock_file.close()
        return len(rows)

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
    files = list(dataset.glob("*/*/*/*/*/*.parquet"))
    catalog = Path(catalog_path).resolve()
    catalog.parent.mkdir(parents=True, exist_ok=True)
    glob = _sql_literal(
        str(dataset / "*" / "*" / "*" / "*" / "*" / "*.parquet")
    )
    connection = duckdb.connect(str(catalog))
    try:
        connection.execute("SET TimeZone = 'UTC'")
        if files:
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


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"
