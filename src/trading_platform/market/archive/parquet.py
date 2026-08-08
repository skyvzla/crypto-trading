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
        self._lock_file = (self.root / ".writer.lock").open("a+")
        self._closed = False
        try:
            fcntl.flock(
                self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB
            )
        except BlockingIOError as error:
            self._lock_file.close()
            raise RuntimeError(
                f"archive writer is already active for {self.root}"
            ) from error

    def __enter__(self) -> "ParquetCandleArchive":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
        self._lock_file.close()

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
            raise ValueError("one Parquet write must contain exactly one archive partition")
        symbol, timeframe, year, month, day = keys.pop()
        partition = (
            self.root
            / f"symbol={symbol}"
            / f"timeframe={timeframe}"
            / f"year={year:04d}"
            / f"month={month:02d}"
        )
        partition /= f"day={day:02d}"
        partition.mkdir(parents=True, exist_ok=True)
        target = partition / "candles.parquet"
        temporary = partition / f".candles-{uuid4().hex}.tmp.parquet"
        table = pa.table(
            {
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
        try:
            pq.write_table(table, temporary, compression="zstd")
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return len(rows)


def create_duckdb_catalog(root: str | Path, catalog_path: str | Path) -> Path:
    dataset = Path(root).resolve()
    files = list(dataset.glob("symbol=*/timeframe=*/year=*/month=*/**/*.parquet"))
    if not files:
        raise ValueError(f"no candle Parquet partitions found under {dataset}")
    catalog = Path(catalog_path).resolve()
    catalog.parent.mkdir(parents=True, exist_ok=True)
    glob = _sql_literal(str(dataset / "symbol=*" / "timeframe=*" / "**" / "*.parquet"))
    connection = duckdb.connect(str(catalog))
    try:
        connection.execute("SET TimeZone = 'UTC'")
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
                {glob}, hive_partitioning = true, union_by_name = true
            )
            """
        )
    finally:
        connection.close()
    return catalog


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"
