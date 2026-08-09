from decimal import Decimal

import duckdb
import pytest

from trading_platform.backtest.loader import BacktestDataLoader
from trading_platform.shared.events import Bar1s, Kline


def _write_candle_archive(path):
    connection = duckdb.connect(str(path))
    try:
        connection.execute(
            """
            CREATE TABLE candles (
                symbol VARCHAR,
                timeframe VARCHAR,
                open_time TIMESTAMPTZ,
                open DOUBLE,
                high DOUBLE,
                low DOUBLE,
                close DOUBLE,
                volume DOUBLE,
                close_time TIMESTAMPTZ
            )
            """
        )
        rows = [
            ("AKEUSDT", "1s", 1_000, 10, 12, 9, 11, 3, 2_000),
            ("AKEUSDT", "1m", 1_000, 10, 13, 8, 12, 30, 60_999),
            ("AKEUSDT", "5m", 1_000, 10, 14, 7, 13, 50, 300_999),
        ]
        connection.executemany(
            """
            INSERT INTO candles VALUES (
                ?, ?, to_timestamp(? / 1000.0), ?, ?, ?, ?, ?,
                to_timestamp(? / 1000.0)
            )
            """,
            rows,
        )
    finally:
        connection.close()


def test_duckdb_loader_reads_candles_without_mutating_archive(tmp_path):
    archive = tmp_path / "history.duckdb"
    _write_candle_archive(archive)
    loader = BacktestDataLoader(
        data_dir="unused",
        duckdb_path=str(archive),
        symbols=["akeusdt"],
        start_ms=0,
        end_ms=400_000,
        require_aggtrades=True,
        required_kline_intervals=["1m", "5m"],
    )

    events = list(loader.iter_all())

    assert len(events) == 3
    bar = next(event for event in events if isinstance(event, Bar1s))
    assert bar.symbol == "AKEUSDT"
    assert bar.timestamp == 1_000
    assert bar.available_time == 2_000
    assert bar.trade_count == 0
    assert bar.vwap == Decimal("11.0")
    assert {
        event.interval for event in events if isinstance(event, Kline)
    } == {"1m", "5m"}

    check = duckdb.connect(str(archive), read_only=True)
    try:
        assert check.execute("SELECT count(*) FROM candles").fetchone()[0] == 3
    finally:
        check.close()


def test_duckdb_stream_matches_materialized_event_order(tmp_path):
    archive = tmp_path / "history.duckdb"
    _write_candle_archive(archive)
    kwargs = {
        "data_dir": "unused",
        "duckdb_path": str(archive),
        "symbols": ["AKEUSDT"],
        "start_ms": 0,
        "end_ms": 400_000,
        "require_aggtrades": True,
        "required_kline_intervals": ["1m", "5m"],
    }

    materialized = list(BacktestDataLoader(**kwargs).iter_all())
    streamed = list(
        BacktestDataLoader(**kwargs).iter_all(
            chunk_hours=0.0003,
            fetch_batch_size=1,
        )
    )

    assert streamed == materialized


def test_duckdb_stream_rejects_missing_required_dataset_before_yield(tmp_path):
    archive = tmp_path / "history.duckdb"
    _write_candle_archive(archive)
    loader = BacktestDataLoader(
        data_dir="unused",
        duckdb_path=str(archive),
        symbols=["AKEUSDT"],
        start_ms=0,
        end_ms=400_000,
        required_kline_intervals=["15m"],
    )

    with pytest.raises(ValueError, match="Missing required 15m"):
        list(loader.iter_all())


def test_duckdb_loader_applies_explicit_one_second_time_shift(tmp_path):
    archive = tmp_path / "history.duckdb"
    _write_candle_archive(archive)
    loader = BacktestDataLoader(
        data_dir="unused",
        duckdb_path=str(archive),
        symbols=["AKEUSDT"],
        start_ms=0,
        end_ms=100_000,
        bar1s_time_shift_ms=8_000,
    )

    events = list(loader.iter_all())
    bar = next(event for event in events if isinstance(event, Bar1s))

    assert bar.timestamp == 9_000
    assert bar.available_time == 10_000


def test_duckdb_loader_rejects_incompatible_archive(tmp_path):
    archive = tmp_path / "invalid.duckdb"
    connection = duckdb.connect(str(archive))
    connection.execute("CREATE TABLE candles (symbol VARCHAR)")
    connection.close()

    loader = BacktestDataLoader(
        data_dir="unused",
        duckdb_path=str(archive),
        symbols=["AKEUSDT"],
        start_ms=0,
        end_ms=1_000,
    )

    with pytest.raises(ValueError, match="candles missing columns"):
        list(loader.iter_all())


def test_duckdb_loader_rejects_invalid_one_second_duration(tmp_path):
    archive = tmp_path / "invalid-duration.duckdb"
    _write_candle_archive(archive)
    connection = duckdb.connect(str(archive))
    try:
        connection.execute(
            "UPDATE candles SET close_time = to_timestamp(3) "
            "WHERE timeframe = '1s'"
        )
    finally:
        connection.close()

    loader = BacktestDataLoader(
        data_dir="unused",
        duckdb_path=str(archive),
        symbols=["AKEUSDT"],
        start_ms=0,
        end_ms=400_000,
        require_aggtrades=True,
    )

    with pytest.raises(ValueError, match="invalid 1s candle duration"):
        list(loader.iter_all())
