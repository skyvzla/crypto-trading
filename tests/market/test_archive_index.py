from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from trading_platform.market.archive.index import (
    ARCHIVE_INDEX_FILENAME,
    ArchiveIndexError,
    build_archive_index,
    load_archive_index,
)
from trading_platform.market.archive import index as archive_index
from trading_platform.market.archive.parquet import (
    ParquetCandleArchive,
    archive_root_from_catalog,
    create_duckdb_catalog,
)
from trading_platform.market.archive import index_cli
from trading_platform.backtest.loader import BacktestDataLoader


def _table(symbol: str, timeframe: str, start: datetime, rows: int) -> pa.Table:
    step = timedelta(seconds=1) if timeframe == "1s" else timedelta(minutes=1)
    opens = [start + step * offset for offset in range(rows)]
    closes = [value + step - timedelta(milliseconds=1) for value in opens]
    return pa.table({
        "symbol": [symbol] * rows,
        "timeframe": [timeframe] * rows,
        "open_time": pa.array(opens, type=pa.timestamp("ms", tz="UTC")),
        "open": [1.0] * rows,
        "high": [2.0] * rows,
        "low": [0.5] * rows,
        "close": [1.5] * rows,
        "volume": [10.0] * rows,
        "close_time": pa.array(closes, type=pa.timestamp("ms", tz="UTC")),
    })


def test_index_rows_are_written_in_bounded_batches(monkeypatch):
    class RecordingWriter:
        def __init__(self) -> None:
            self.batch_sizes: list[int] = []

        def write_table(self, table: pa.Table) -> None:
            self.batch_sizes.append(len(table))

    rows = (
        {
            "symbol": "AKEUSDT",
            "timeframe": "1s",
            "year": 2026,
            "month": 7,
            "day": day,
            "relative_path": f"AKEUSDT/1s/2026/07/{day:02d}/candles.parquet",
            "row_count": 1,
            "first_open_ms": day,
            "last_close_ms": day,
            "file_size": 1,
            "file_mtime_ns": 1,
        }
        for day in range(1, 6)
    )
    writer = RecordingWriter()
    monkeypatch.setattr(archive_index, "INDEX_WRITE_BATCH_SIZE", 2)

    count = archive_index._write_index_rows(
        writer, rows, schema=archive_index.INDEX_SCHEMA
    )

    assert count == 5
    assert writer.batch_sizes == [2, 2, 1]


def test_build_archive_index_reads_partition_footer_in_parallel(tmp_path: Path):
    root = tmp_path / "history"
    with ParquetCandleArchive(root, rebuild_index_on_close=False) as archive:
        archive.upsert_table(
            _table("AKEUSDT", "1s", datetime(2026, 7, 1, tzinfo=UTC), 3),
            symbol="AKEUSDT", timeframe="1s", year=2026, month=7, day=1,
        )
        archive.upsert_table(
            _table("AKEUSDT", "1m", datetime(2026, 7, 1, tzinfo=UTC), 2),
            symbol="AKEUSDT", timeframe="1m", year=2026, month=7, day=0,
        )

    index_path = build_archive_index(root, workers=2)
    frame = load_archive_index(index_path, verify_files=True)

    assert index_path == root / ARCHIVE_INDEX_FILENAME
    assert frame[["timeframe", "row_count"]].sort_values("timeframe").values.tolist() == [
        ["1m", 2], ["1s", 3]
    ]
    one_second = frame[frame["timeframe"] == "1s"].iloc[0]
    assert one_second["first_open_ms"] == 1782864000000
    assert one_second["last_close_ms"] == 1782864002999


def test_archive_close_atomically_refreshes_index(tmp_path: Path):
    root = tmp_path / "history"
    with ParquetCandleArchive(root, index_workers=1) as archive:
        archive.upsert_table(
            _table("BANKUSDT", "1s", datetime(2026, 7, 2, tzinfo=UTC), 1),
            symbol="BANKUSDT", timeframe="1s", year=2026, month=7, day=2,
        )

    frame = load_archive_index(root / ARCHIVE_INDEX_FILENAME)

    assert frame.iloc[0]["symbol"] == "BANKUSDT"


def test_archive_index_rejects_changed_partition(tmp_path: Path):
    root = tmp_path / "history"
    with ParquetCandleArchive(root, index_workers=1) as archive:
        archive.upsert_table(
            _table("AKEUSDT", "1s", datetime(2026, 7, 1, tzinfo=UTC), 1),
            symbol="AKEUSDT", timeframe="1s", year=2026, month=7, day=1,
        )
    partition = root / "AKEUSDT/1s/2026/07/01/candles.parquet"
    table = pq.read_table(partition)
    pq.write_table(pa.concat_tables([table, table]), partition)

    with pytest.raises(ArchiveIndexError, match="stale"):
        load_archive_index(root / ARCHIVE_INDEX_FILENAME, verify_files=True)


def test_archive_index_rejects_incomplete_atomic_generation(tmp_path: Path):
    root = tmp_path / "history"
    index_path = build_archive_index(root, workers=1)
    index_path.with_name("archive_index.meta.json").unlink()

    with pytest.raises(ArchiveIndexError, match="missing"):
        load_archive_index(index_path)


def test_catalog_records_archive_root_and_index(tmp_path: Path):
    root = tmp_path / "history"
    root.mkdir()
    build_archive_index(root, workers=1)

    catalog = create_duckdb_catalog(root, tmp_path / "history.duckdb")

    assert archive_root_from_catalog(catalog) == root.resolve()


def test_indexed_loader_validates_required_datasets_without_catalog_scan(
    tmp_path: Path,
):
    root = tmp_path / "history"
    with ParquetCandleArchive(root, index_workers=1) as archive:
        for timeframe in ("1s", "1m", "5m"):
            archive.upsert_table(
                _table(
                    "AKEUSDT", timeframe, datetime(2026, 7, 1, tzinfo=UTC), 2
                ),
                symbol="AKEUSDT", timeframe=timeframe,
                year=2026, month=7, day=1 if timeframe == "1s" else 0,
            )
    catalog = create_duckdb_catalog(root, tmp_path / "history.duckdb")
    loader = BacktestDataLoader(
        data_dir="unused",
        duckdb_path=str(catalog),
        archive_index_path=str(root / ARCHIVE_INDEX_FILENAME),
        symbols=["AKEUSDT"],
        start_ms=1782864000000,
        end_ms=1782864120000,
        require_aggtrades=True,
        required_kline_intervals=["1m", "5m"],
    )

    loader._validate_stream_datasets_from_index()


def test_index_cli_rebuilds_catalog_metadata(tmp_path: Path, capsys):
    root = tmp_path / "history"
    root.mkdir()
    catalog = tmp_path / "history.duckdb"

    assert index_cli.main([
        str(root), "--catalog", str(catalog), "--workers", "1"
    ]) == 0

    assert archive_root_from_catalog(catalog) == root.resolve()
    assert "分区=0" in capsys.readouterr().out
