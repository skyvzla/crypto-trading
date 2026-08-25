from datetime import UTC, datetime, timedelta
from decimal import Decimal

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from trading_platform.backtest.loader import BacktestDataLoader, MetricsDataLoader
from trading_platform.market.archive.index import build_archive_index
from trading_platform.market.archive.parquet import create_duckdb_catalog
from trading_platform.market.archive.metrics import MetricsArchive, MetricsSnapshot
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


def test_duckdb_loader_supports_explicit_empty_one_second_projection(tmp_path):
    archive = tmp_path / "history.duckdb"
    _write_candle_archive(archive)

    events = list(BacktestDataLoader(
        duckdb_path=str(archive),
        symbols=["AKEUSDT"],
        start_ms=0,
        end_ms=400_000,
        require_aggtrades=True,
        bar1s_feature_columns=[],
    ).iter_all())

    bar = next(event for event in events if isinstance(event, Bar1s))
    assert bar.trade_count == 0
    assert bar.vwap == Decimal("11.0")
    assert bar.quote_volume is None


def test_duckdb_loader_rejects_unknown_projected_one_second_feature():
    with pytest.raises(ValueError, match="unknown projected 1s feature columns"):
        BacktestDataLoader(
            duckdb_path="missing.duckdb",
            symbols=["AKEUSDT"],
            start_ms=0,
            end_ms=1_000,
            bar1s_feature_columns=["not_a_feature"],
        )


def test_duckdb_loader_canonicalizes_projected_feature_order(tmp_path):
    archive = tmp_path / "projected.duckdb"
    connection = duckdb.connect(str(archive))
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
                close_time TIMESTAMPTZ,
                vwap DOUBLE,
                quote_volume DOUBLE,
                trade_count BIGINT
            )
            """
        )
        connection.execute(
            """
            INSERT INTO candles VALUES (
                'AKEUSDT', '1s', to_timestamp(1000 / 1000.0),
                100, 102, 99, 101, 5, to_timestamp(1999 / 1000.0),
                101.25, 404.5, 7
            )
            """
        )
    finally:
        connection.close()

    event = next(iter(BacktestDataLoader(
        duckdb_path=str(archive),
        symbols=["AKEUSDT"],
        start_ms=0,
        end_ms=4_000,
        require_aggtrades=True,
        bar1s_feature_columns=["trade_count", "vwap", "quote_volume"],
    ).iter_all()))

    assert isinstance(event, Bar1s)
    assert event.trade_count == 7
    assert event.vwap == Decimal("101.25")
    assert event.quote_volume == Decimal("404.5")


def test_duckdb_loader_rejects_missing_projected_feature_before_yield(tmp_path):
    archive = tmp_path / "missing-feature.duckdb"
    _write_candle_archive(archive)
    loader = BacktestDataLoader(
        duckdb_path=str(archive),
        symbols=["AKEUSDT"],
        start_ms=0,
        end_ms=4_000,
        require_aggtrades=True,
        bar1s_feature_columns=["trade_count"],
    )

    with pytest.raises(ValueError, match="projected/requested 1s feature columns"):
        list(loader.iter_all())


def test_duckdb_loader_checks_projected_features_in_physical_archive_schema(tmp_path):
    root = tmp_path / "history"
    start = datetime(2026, 8, 1, tzinfo=UTC)
    base_columns = {
        "symbol": ["AKEUSDT"],
        "timeframe": ["1s"],
        "open_time": pa.array([start], type=pa.timestamp("ms", tz="UTC")),
        "open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0],
        "volume": [1.0],
        "close_time": pa.array([
            start.replace(microsecond=999_000)
        ], type=pa.timestamp("ms", tz="UTC")),
    }
    first_partition = root / "AKEUSDT" / "1s" / "2026" / "08" / "01"
    first_partition.mkdir(parents=True)
    pq.write_table(pa.table(base_columns), first_partition / "candles.parquet")
    second_start = start + timedelta(days=1)
    second_columns = {
        **base_columns,
        "open_time": pa.array([second_start], type=pa.timestamp("ms", tz="UTC")),
        "close_time": pa.array([
            second_start.replace(microsecond=999_000)
        ], type=pa.timestamp("ms", tz="UTC")),
        "trade_count": [1],
    }
    second_partition = root / "AKEUSDT" / "1s" / "2026" / "08" / "02"
    second_partition.mkdir(parents=True)
    pq.write_table(pa.table(second_columns), second_partition / "candles.parquet")
    build_archive_index(root, workers=1)
    catalog = create_duckdb_catalog(root, tmp_path / "history.duckdb")

    loader = BacktestDataLoader(
        duckdb_path=str(catalog),
        symbols=["AKEUSDT"],
        start_ms=int(start.timestamp() * 1_000),
        end_ms=int((start + timedelta(days=2)).timestamp() * 1_000),
        require_aggtrades=True,
        bar1s_feature_columns=["trade_count"],
    )

    with pytest.raises(ValueError, match="physical source schema"):
        list(loader.iter_all())


def test_duckdb_loader_full_and_projected_events_preserve_requested_values(tmp_path):
    archive = tmp_path / "full-vs-projected.duckdb"
    connection = duckdb.connect(str(archive))
    try:
        connection.execute(
            """
            CREATE TABLE candles (
                symbol VARCHAR, timeframe VARCHAR, open_time TIMESTAMPTZ,
                open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE,
                volume DOUBLE, close_time TIMESTAMPTZ, vwap DOUBLE,
                quote_volume DOUBLE, trade_count BIGINT, first_trade_id BIGINT
            )
            """
        )
        connection.execute(
            """
            INSERT INTO candles VALUES
                ('AKEUSDT', '1s', to_timestamp(1000 / 1000.0),
                 100, 102, 99, 101, 5, to_timestamp(1999 / 1000.0),
                 101.25, 404.5, 7, 12345),
                ('AKEUSDT', '1s', to_timestamp(2000 / 1000.0),
                 101, 103, 100, 102, 6, to_timestamp(2999 / 1000.0),
                 102.25, 505.5, 8, 12346),
                ('AKEUSDT', '1m', to_timestamp(1000 / 1000.0),
                 100, 104, 98, 103, 30, to_timestamp(60999 / 1000.0),
                 NULL, NULL, NULL, NULL)
            """
        )
    finally:
        connection.close()

    full = list(BacktestDataLoader(
        duckdb_path=str(archive), symbols=["AKEUSDT"], start_ms=0, end_ms=100_000,
        require_aggtrades=True, required_kline_intervals=["1m"],
    ).iter_all())
    projected = list(BacktestDataLoader(
        duckdb_path=str(archive), symbols=["AKEUSDT"], start_ms=0, end_ms=100_000,
        require_aggtrades=True, required_kline_intervals=["1m"],
        bar1s_feature_columns=["vwap", "trade_count"],
    ).iter_all())

    assert [type(event) for event in projected] == [type(event) for event in full]
    assert len(full) == 3
    for expected, actual in zip(full, projected, strict=True):
        if isinstance(expected, Bar1s):
            assert isinstance(actual, Bar1s)
            assert (
                actual.symbol, actual.timestamp, actual.available_time,
                actual.open, actual.high, actual.low, actual.close, actual.volume,
                actual.type_priority, actual.sequence,
                actual.trade_count, actual.vwap,
            ) == (
                expected.symbol, expected.timestamp, expected.available_time,
                expected.open, expected.high, expected.low, expected.close,
                expected.volume, expected.type_priority, expected.sequence,
                expected.trade_count, expected.vwap,
            )
            for feature_name in (
                "quote_volume", "raw_trade_count", "taker_buy_volume",
                "taker_sell_volume", "taker_buy_quote_volume",
                "taker_sell_quote_volume", "taker_buy_trade_count",
                "taker_sell_trade_count", "taker_buy_agg_trade_count",
                "taker_sell_agg_trade_count", "max_agg_trade_quantity",
                "max_taker_buy_agg_trade_quantity",
                "max_taker_sell_agg_trade_quantity",
                "first_aggregate_trade_id", "last_aggregate_trade_id",
                "first_trade_id", "last_trade_id",
            ):
                assert getattr(actual, feature_name) is None
        else:
            assert isinstance(expected, Kline)
            assert isinstance(actual, Kline)
            assert actual == expected
    assert [event.timestamp for event in full if isinstance(event, Bar1s)] == [
        1_000, 2_000
    ]
    assert [event.trade_count for event in full if isinstance(event, Bar1s)] == [7, 8]
    assert [event.quote_volume for event in full if isinstance(event, Bar1s)] == [
        Decimal("404.5"), Decimal("505.5")
    ]
    assert [event.first_trade_id for event in full if isinstance(event, Bar1s)] == [
        12345, 12346
    ]


def test_metrics_loader_uses_available_time_for_strategy_visibility(tmp_path):
    metrics_root = tmp_path / "metrics"
    snapshot_time = datetime(2026, 8, 10, tzinfo=UTC)
    available_time = snapshot_time + timedelta(minutes=5)
    with MetricsArchive(metrics_root, index_workers=1) as archive:
        archive.upsert([
            MetricsSnapshot(
                symbol="AKEUSDT",
                snapshot_time=snapshot_time,
                available_time=available_time,
                sum_open_interest=100.0,
                sum_open_interest_value=100.0,
                count_toptrader_long_short_ratio=1.0,
                sum_toptrader_long_short_ratio=1.0,
                count_long_short_ratio=1.2,
                sum_taker_long_short_vol_ratio=1.0,
            )
        ])

    available_ms = int(available_time.timestamp() * 1_000)
    assert MetricsDataLoader(metrics_root, symbol="AKEUSDT").load() == [
        (available_ms, 100.0, 1.2)
    ]
    assert MetricsDataLoader(
        metrics_root, symbol="AKEUSDT", start_ms=available_ms
    ).load() == [(available_ms, 100.0, 1.2)]
    assert MetricsDataLoader(
        metrics_root, symbol="AKEUSDT", end_ms=available_ms
    ).load() == []


def test_duckdb_stream_matches_materialized_event_order(tmp_path):
    archive = tmp_path / "history.duckdb"
    _write_candle_archive(archive)
    kwargs = {
        "duckdb_path": str(archive),
        "symbols": ["AKEUSDT"],
        "start_ms": 0,
        "end_ms": 400_000,
        "require_aggtrades": True,
        "required_kline_intervals": ["1m", "5m"],
    }

    first = list(BacktestDataLoader(**kwargs).iter_all())
    streamed = list(
        BacktestDataLoader(**kwargs).iter_all(
            chunk_hours=0.0003,
            fetch_batch_size=1,
        )
    )

    assert streamed == first


def test_duckdb_loader_can_run_without_one_second_data(tmp_path):
    archive = tmp_path / "kline-only.duckdb"
    connection = duckdb.connect(str(archive))
    try:
        connection.execute(
            """
            CREATE TABLE candles AS SELECT * FROM (VALUES
                ('AKEUSDT', '1m', to_timestamp(1000 / 1000.0), 10.0, 12.0, 9.0, 11.0, 3.0, to_timestamp(60999 / 1000.0)),
                ('AKEUSDT', '5m', to_timestamp(1000 / 1000.0), 10.0, 14.0, 7.0, 13.0, 5.0, to_timestamp(300999 / 1000.0))
            ) AS t(symbol, timeframe, open_time, open, high, low, close, volume, close_time)
            """
        )
    finally:
        connection.close()

    events = list(BacktestDataLoader(
        duckdb_path=str(archive), symbols=["AKEUSDT"], start_ms=0,
        end_ms=400_000, required_kline_intervals=["1m", "5m"]
    ).iter_all())
    assert all(isinstance(event, Kline) for event in events)


def test_duckdb_stream_rejects_missing_required_dataset_before_yield(tmp_path):
    archive = tmp_path / "history.duckdb"
    _write_candle_archive(archive)
    loader = BacktestDataLoader(
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
        duckdb_path=str(archive),
        symbols=["AKEUSDT"],
        start_ms=0,
        end_ms=100_000,
        require_aggtrades=True,
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
        duckdb_path=str(archive),
        symbols=["AKEUSDT"],
        start_ms=0,
        end_ms=400_000,
        require_aggtrades=True,
    )

    with pytest.raises(ValueError, match="invalid 1s candle duration"):
        list(loader.iter_all())
