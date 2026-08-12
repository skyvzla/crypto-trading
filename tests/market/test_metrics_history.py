from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from io import BytesIO
import json
from threading import Barrier, Event
import zipfile

import duckdb
import pyarrow.parquet as pq
import pytest

from trading_platform.market.archive import ArchiveNotFoundError, DownloadResult
from trading_platform.market.archive.metrics import (
    METRICS_INDEX_FILENAME,
    METRICS_PERIOD,
    MetricsArchive,
    MetricsArchiveIndexError,
    build_metrics_index,
    create_metrics_catalog,
    download_metrics_history,
    load_metrics_index,
    metrics_archive_url,
    parse_metrics_archive,
    publish_metrics_archive,
)
from trading_platform.market.archive import cli as archive_cli


HEADER = (
    "create_time,symbol,sum_open_interest,sum_open_interest_value,"
    "count_toptrader_long_short_ratio,sum_toptrader_long_short_ratio,"
    "count_long_short_ratio,sum_taker_long_short_vol_ratio\n"
)


def _archive_bytes(day: str, rows: list[str], *, symbol: str = "BTCUSDT") -> bytes:
    payload = BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr(
            f"{symbol}-metrics-{day}.csv",
            HEADER + "\n".join(rows) + "\n",
        )
    return payload.getvalue()


def _row(
    timestamp: str,
    *,
    symbol: str = "BTCUSDT",
    oi: str = "100",
    oi_value: str = "1000",
    top_account: str = "1.1",
    top_position: str = "1.2",
    global_ratio: str = "1.3",
    taker_ratio: str = "1.4",
) -> str:
    return ",".join([
        timestamp,
        symbol,
        oi,
        oi_value,
        top_account,
        top_position,
        global_ratio,
        taker_ratio,
    ])


def test_metrics_url_uses_usdm_daily_vision_archive():
    assert metrics_archive_url("btcusdt", "2026-08-10") == (
        "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision/"
        "data/futures/um/daily/metrics/BTCUSDT/"
        "BTCUSDT-metrics-2026-08-10.zip"
    )


def test_metrics_parser_sorts_unordered_rows_deduplicates_exact_rows_and_keeps_nulls():
    content = _archive_bytes("2026-08-10", [
        _row("2026-08-10 00:10:00", top_account="", top_position="", global_ratio="", taker_ratio=""),
        _row("2026-08-10 00:00:00"),
        _row("2026-08-10 00:10:00", top_account="", top_position="", global_ratio="", taker_ratio=""),
        _row("2026-08-10 00:05:00"),
    ])

    snapshots = parse_metrics_archive(content, "btcusdt", "2026-08-10")

    assert [item.snapshot_time for item in snapshots] == [
        datetime(2026, 8, 10, 0, 0, tzinfo=UTC),
        datetime(2026, 8, 10, 0, 5, tzinfo=UTC),
        datetime(2026, 8, 10, 0, 10, tzinfo=UTC),
    ]
    assert snapshots[-1].sum_open_interest == 100.0
    assert snapshots[-1].count_toptrader_long_short_ratio is None
    assert snapshots[-1].quality_status == "partial"
    assert snapshots[0].available_time == datetime(2026, 8, 10, 0, 5, tzinfo=UTC)


def test_metrics_parser_rejects_conflicting_same_snapshot_rows():
    content = _archive_bytes("2026-08-10", [
        _row("2026-08-10 00:00:00", oi="100"),
        _row("2026-08-10 00:00:00", oi="101"),
    ])

    with pytest.raises(ValueError, match="conflicting rows"):
        parse_metrics_archive(content, "BTCUSDT", date(2026, 8, 10))


def test_metrics_parser_accepts_next_day_midnight_source_boundary():
    content = _archive_bytes("2026-08-10", [
        _row("2026-08-10 23:55:00"),
        _row("2026-08-11 00:00:02"),
    ])

    snapshots = parse_metrics_archive(content, "BTCUSDT", "2026-08-10")

    assert [item.snapshot_time for item in snapshots] == [
        datetime(2026, 8, 10, 23, 55, tzinfo=UTC),
        datetime(2026, 8, 11, 0, 0, 2, tzinfo=UTC),
    ]


def test_metrics_parser_preserves_source_timestamp_with_collection_delay():
    content = _archive_bytes("2026-02-20", [
        _row("2026-02-20 21:40:02", symbol="0GUSDT"),
    ], symbol="0GUSDT")

    snapshots = parse_metrics_archive(content, "0GUSDT", "2026-02-20")

    assert snapshots[0].snapshot_time == datetime(
        2026, 2, 20, 21, 40, 2, tzinfo=UTC
    )
    assert snapshots[0].available_time == datetime(
        2026, 2, 20, 21, 45, 2, tzinfo=UTC
    )


def test_metrics_parser_error_identifies_source_row_and_timestamp():
    content = _archive_bytes("2026-08-10", [
        _row("not-a-timestamp"),
    ])

    with pytest.raises(
        ValueError,
        match=(
            r"BTCUSDT-metrics-2026-08-10\.csv:2 "
            r"create_time='not-a-timestamp'"
        ),
    ):
        parse_metrics_archive(content, "BTCUSDT", "2026-08-10")


def test_metrics_archive_rejects_source_partition_beyond_next_day_midnight(tmp_path):
    snapshots = parse_metrics_archive(
        _archive_bytes("2026-08-10", [_row("2026-08-11 00:00:00")]),
        "BTCUSDT",
        "2026-08-10",
    )

    with MetricsArchive(tmp_path / "metrics") as archive:
        with pytest.raises(ValueError, match="outside their source-day partition"):
            archive.upsert(snapshots, partition_day=date(2026, 8, 9))


@pytest.mark.parametrize("row", [
    _row("not-a-timestamp"),
    _row("2026-08-11 00:05:00"),
    "2026-08-10 00:00:00,ETHUSDT,100,1000,1.1,1.2,1.3,1.4",
])
def test_metrics_parser_rejects_invalid_timestamp_partition_or_symbol(row):
    content = _archive_bytes("2026-08-10", [row])

    with pytest.raises(ValueError):
        parse_metrics_archive(content, "BTCUSDT", "2026-08-10")


def test_metrics_downloader_writes_daily_parquet_skips_existing_and_catalog_is_read_only(tmp_path):
    requested: list[str] = []
    content = _archive_bytes("2026-08-10", [
        _row("2026-08-10 00:05:00"),
        _row("2026-08-10 00:00:00"),
        _row("2026-08-11 00:00:00"),
    ])

    def fetch(url: str) -> bytes:
        requested.append(url)
        return content

    root = tmp_path / "metrics"
    with MetricsArchive(root, index_workers=1) as archive:
        results = download_metrics_history(
            archive,
            fetch=fetch,
            symbols=["BTCUSDT"],
            start=datetime(2026, 8, 10, tzinfo=UTC),
            end=datetime(2026, 8, 11, tzinfo=UTC),
        )

    partition = root / "usdm/BTCUSDT/2026/08/10/metrics.parquet"
    assert partition.is_file()
    assert results[0].rows == 3
    assert requested == [metrics_archive_url("BTCUSDT", "2026-08-10")]
    assert pq.read_table(partition).column_names == [field.name for field in pq.ParquetFile(partition).schema_arrow]
    assert [item.as_py() for item in pq.read_table(partition)["snapshot_time"]] == [
        datetime(2026, 8, 10, 0, 0, tzinfo=UTC),
        datetime(2026, 8, 10, 0, 5, tzinfo=UTC),
        datetime(2026, 8, 11, 0, 0, tzinfo=UTC),
    ]
    assert not (root / "usdm/BTCUSDT/2026/08/11/metrics.parquet").exists()
    assert load_metrics_index(root, verify_files=True).num_rows == 1

    with MetricsArchive(root, index_workers=1) as archive:
        repeated = download_metrics_history(
            archive,
            fetch=lambda _url: pytest.fail("existing metrics partition was downloaded"),
            symbols=["BTCUSDT"],
            start=datetime(2026, 8, 10, tzinfo=UTC),
            end=datetime(2026, 8, 11, tzinfo=UTC),
        )
    assert repeated[0].skipped is True

    catalog = create_metrics_catalog(root, tmp_path / "metrics.duckdb")
    connection = duckdb.connect(str(catalog), read_only=True)
    try:
        rows = connection.execute(
            "SELECT symbol, period, epoch_ms(snapshot_time), quality_status "
            "FROM metrics ORDER BY snapshot_time"
        ).fetchall()
    finally:
        connection.close()
    assert rows == [
        ("BTCUSDT", "5m", 1786320000000, "complete"),
        ("BTCUSDT", "5m", 1786320300000, "complete"),
        ("BTCUSDT", "5m", 1786406400000, "complete"),
    ]


def test_metrics_downloader_marks_missing_vision_days_unavailable(tmp_path):
    root = tmp_path / "metrics"
    with MetricsArchive(root, index_workers=1) as archive:
        result = download_metrics_history(
            archive,
            fetch=lambda _url: (_ for _ in ()).throw(ArchiveNotFoundError()),
            symbols=["BTCUSDT"],
            start=datetime(2026, 8, 10, tzinfo=UTC),
            end=datetime(2026, 8, 11, tzinfo=UTC),
        )
    catalog = create_metrics_catalog(root, root / "metrics.duckdb")

    assert result[0].unavailable is True
    assert not (root / "metrics").exists()
    assert not (root / METRICS_INDEX_FILENAME).exists()
    connection = duckdb.connect(str(catalog), read_only=True)
    try:
        assert connection.execute("SELECT * FROM metrics").fetchall() == []
    finally:
        connection.close()


def test_metrics_index_rejects_modified_partition(tmp_path):
    root = tmp_path / "metrics"
    snapshots = parse_metrics_archive(
        _archive_bytes("2026-08-10", [_row("2026-08-10 00:00:00")]),
        "BTCUSDT",
        "2026-08-10",
    )
    with MetricsArchive(root, rebuild_index_on_close=False) as archive:
        archive.upsert(snapshots)
    build_metrics_index(root)
    partition = root / "usdm/BTCUSDT/2026/08/10/metrics.parquet"
    table = pq.read_table(partition)
    pq.write_table(table, partition)

    with pytest.raises(MetricsArchiveIndexError, match="stale"):
        load_metrics_index(root, verify_files=True)


def test_metrics_archives_write_independent_daily_partitions(tmp_path):
    root = tmp_path / "metrics"
    first_day = parse_metrics_archive(
        _archive_bytes("2026-08-10", [_row("2026-08-10 00:00:00")]),
        "BTCUSDT",
        "2026-08-10",
    )
    second_day = parse_metrics_archive(
        _archive_bytes("2026-08-11", [_row("2026-08-11 00:00:00")]),
        "BTCUSDT",
        "2026-08-11",
    )

    with MetricsArchive(root, rebuild_index_on_close=False) as first:
        with MetricsArchive(root, rebuild_index_on_close=False) as second:
            assert first.upsert(first_day) == 1
            assert second.upsert(second_day) == 1

    assert (root / "usdm/BTCUSDT/2026/08/10/metrics.parquet").is_file()
    assert (root / "usdm/BTCUSDT/2026/08/11/metrics.parquet").is_file()


def test_metrics_archive_publish_refreshes_index_and_catalog_once(tmp_path, monkeypatch):
    root = tmp_path / "metrics"
    snapshots = parse_metrics_archive(
        _archive_bytes("2026-08-10", [_row("2026-08-10 00:00:00")]),
        "BTCUSDT",
        "2026-08-10",
    )
    publish_calls = 0
    original_publish = publish_metrics_archive

    def recording_publish(*args, **kwargs):
        nonlocal publish_calls
        publish_calls += 1
        return original_publish(*args, **kwargs)

    monkeypatch.setattr(
        "trading_platform.market.archive.metrics.publish_metrics_archive",
        recording_publish,
    )
    with MetricsArchive(root, index_workers=1) as archive:
        archive.upsert(snapshots)
        index, catalog = archive.publish(root / "metrics.duckdb")

    assert publish_calls == 1
    assert index.is_file()
    assert catalog.is_file()
    assert load_metrics_index(root, verify_files=True).num_rows == 1


def test_metrics_archive_publish_lock_blocks_new_partition_write(tmp_path, monkeypatch):
    root = tmp_path / "metrics"
    snapshots = parse_metrics_archive(
        _archive_bytes("2026-08-10", [_row("2026-08-10 00:00:00")]),
        "BTCUSDT",
        "2026-08-10",
    )
    original_build = __import__(
        "trading_platform.market.archive.metrics", fromlist=["_build_metrics_index"]
    )._build_metrics_index
    publishing = Event()
    release_publish = Event()

    def blocked_build(*args, **kwargs):
        publishing.set()
        assert release_publish.wait(timeout=1)
        return original_build(*args, **kwargs)

    monkeypatch.setattr(
        "trading_platform.market.archive.metrics._build_metrics_index",
        blocked_build,
    )

    with MetricsArchive(root, rebuild_index_on_close=False) as archive:
        archive.upsert(snapshots)
    publisher = ThreadPoolExecutor(max_workers=1)
    try:
        future = publisher.submit(
            publish_metrics_archive,
            root,
            root / "metrics.duckdb",
        )
        assert publishing.wait(timeout=1)
        with MetricsArchive(root, rebuild_index_on_close=False) as writer:
            write_future = ThreadPoolExecutor(max_workers=1)
            try:
                pending_write = write_future.submit(writer.upsert, snapshots)
                assert not pending_write.done()
                release_publish.set()
                future.result(timeout=1)
                assert pending_write.result(timeout=1) == 1
            finally:
                write_future.shutdown(wait=True)
    finally:
        publisher.shutdown(wait=True)


def test_metrics_downloader_processes_distinct_partitions_concurrently(tmp_path):
    barrier = Barrier(2)

    def fetch(url: str) -> bytes:
        day = url.removesuffix(".zip").rsplit("-", 3)[-3:]
        partition_day = "-".join(day)
        barrier.wait(timeout=1)
        return _archive_bytes(
            partition_day,
            [_row(f"{partition_day} 00:00:00")],
        )

    root = tmp_path / "metrics"
    with MetricsArchive(root, index_workers=1) as archive:
        results = download_metrics_history(
            archive,
            fetch=fetch,
            symbols=["BTCUSDT"],
            start=datetime(2026, 8, 10, tzinfo=UTC),
            end=datetime(2026, 8, 12, tzinfo=UTC),
            max_workers=2,
        )

    assert [item.rows for item in results] == [1, 1]


def test_market_archive_downloads_metrics_by_default_to_sibling_metrics_directory(
    tmp_path, monkeypatch
):
    captured: dict[str, object] = {}

    def fake_candle_download(_archive, **_kwargs):
        return []

    def fake_metrics_download(archive, **kwargs):
        captured["metrics_archive"] = archive.root
        captured.update(kwargs)
        return []

    monkeypatch.setattr(
        archive_cli,
        "BinanceFuturesMetadataFetcher",
        lambda *_args, **_kwargs: lambda _symbols: {},
    )
    monkeypatch.setattr(archive_cli, "download_history", fake_candle_download)
    monkeypatch.setattr(
        archive_cli,
        "download_metrics_history",
        fake_metrics_download,
    )
    monkeypatch.setattr(
        archive_cli,
        "create_duckdb_catalog",
        lambda _archive, catalog: catalog,
    )
    monkeypatch.setattr(
        archive_cli.MetricsArchive,
        "publish",
        lambda _archive, catalog: (_archive.root / METRICS_INDEX_FILENAME, catalog),
    )
    exit_code = archive_cli.main([
        str(tmp_path / "candles"),
        "--symbols", "BTCUSDT",
        "--timeframes", "1m",
        "--start", "2026-08-10T00:00:00Z",
        "--end", "2026-08-11T00:00:00Z",
        "--min-free-gb", "0",
        "--workers", "1",
    ])

    assert exit_code == 0
    assert captured["symbols"] == ["BTCUSDT"]
    assert captured["start"] == datetime(2026, 8, 10, tzinfo=UTC)
    assert captured["end"] == datetime(2026, 8, 11, tzinfo=UTC)
    assert captured["max_workers"] == 1
    assert captured["metrics_archive"] == (tmp_path / "metrics").resolve()


def test_market_archive_starts_candles_and_metrics_concurrently_with_split_worker_budget(
    tmp_path, monkeypatch
):
    both_started = Barrier(2)
    candle_started = Event()
    metrics_started = Event()
    captured: dict[str, int] = {}

    def fake_candle_download(_archive, **kwargs):
        captured["candle_workers"] = kwargs["max_workers"]
        candle_started.set()
        both_started.wait(timeout=1)
        return []

    def fake_metrics_download(_archive, **kwargs):
        captured["metrics_workers"] = kwargs["max_workers"]
        metrics_started.set()
        both_started.wait(timeout=1)
        return []

    monkeypatch.setattr(
        archive_cli,
        "BinanceFuturesMetadataFetcher",
        lambda *_args, **_kwargs: lambda _symbols: {},
    )
    monkeypatch.setattr(archive_cli, "download_history", fake_candle_download)
    monkeypatch.setattr(
        archive_cli,
        "download_metrics_history",
        fake_metrics_download,
    )
    monkeypatch.setattr(
        archive_cli,
        "create_duckdb_catalog",
        lambda _archive, catalog: catalog,
    )
    monkeypatch.setattr(
        archive_cli.MetricsArchive,
        "publish",
        lambda _archive, catalog: (_archive.root / METRICS_INDEX_FILENAME, catalog),
    )

    assert archive_cli.main([
        str(tmp_path / "candles"),
        "--symbols", "BTCUSDT",
        "--timeframes", "1m",
        "--start", "2026-08-10T00:00:00Z",
        "--end", "2026-08-11T00:00:00Z",
        "--min-free-gb", "0",
        "--workers", "4",
    ]) == 0

    assert candle_started.is_set()
    assert metrics_started.is_set()
    assert captured == {"candle_workers": 2, "metrics_workers": 2}


@pytest.mark.parametrize(
    ("workers", "expected"),
    [(1, (1, 1)), (2, (1, 1)), (3, (2, 1)), (4, (2, 2)), (5, (3, 2))],
)
def test_market_archive_splits_total_download_worker_budget(workers, expected):
    assert archive_cli._split_download_workers(workers) == expected


@pytest.mark.parametrize(
    "metrics_path",
    ["candles", "candles/metrics", "."],
)
def test_market_archive_rejects_non_distinct_metrics_root(
    tmp_path, metrics_path, monkeypatch
):
    monkeypatch.setattr(
        archive_cli,
        "BinanceFuturesMetadataFetcher",
        lambda *_args, **_kwargs: lambda _symbols: {},
    )
    candles = tmp_path / "candles"

    assert archive_cli.main([
        str(candles),
        "--symbols", "BTCUSDT",
        "--timeframes", "1m",
        "--start", "2026-08-10T00:00:00Z",
        "--end", "2026-08-11T00:00:00Z",
        "--min-free-gb", "0",
        "--metrics-archive", str(tmp_path / metrics_path),
    ]) == 1


def test_market_archive_without_metrics_does_not_start_metrics_download(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        archive_cli,
        "BinanceFuturesMetadataFetcher",
        lambda *_args, **_kwargs: lambda _symbols: {},
    )
    monkeypatch.setattr(archive_cli, "download_history", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        archive_cli,
        "download_metrics_history",
        lambda *_args, **_kwargs: pytest.fail("metrics should be disabled"),
    )
    monkeypatch.setattr(
        archive_cli,
        "create_duckdb_catalog",
        lambda _archive, catalog: catalog,
    )

    assert archive_cli.main([
        str(tmp_path / "candles"),
        "--symbols", "BTCUSDT",
        "--timeframes", "1m",
        "--start", "2026-08-10T00:00:00Z",
        "--end", "2026-08-11T00:00:00Z",
        "--min-free-gb", "0",
        "--without-metrics",
    ]) == 0


def test_market_history_json_result_reports_fixed_metrics_period_and_partition_date(
    capsys, tmp_path
):
    archive_cli._print_result(
        [],
        tmp_path / "candles",
        tmp_path / "candles.duckdb",
        metrics_results=[
            DownloadResult("BTCUSDT", "metrics", "2026-08-10", 288)
        ],
        metrics_archive_path=tmp_path / "metrics",
        metrics_catalog_path=tmp_path / "metrics" / "metrics.duckdb",
        as_json=True,
    )

    payload = json.loads(capsys.readouterr().out)

    assert payload["imports"] == [{
        "symbol": "BTCUSDT",
        "dataset": "metrics",
        "period": METRICS_PERIOD,
        "partition_date": "2026-08-10",
        "rows": 288,
        "skipped": False,
        "unavailable": False,
    }]


def test_metrics_catalog_supports_all_unavailable_download(tmp_path):
    catalog = create_metrics_catalog(tmp_path / "metrics", tmp_path / "catalog.duckdb")
    connection = duckdb.connect(str(catalog), read_only=True)
    try:
        rows = connection.execute("SELECT * FROM metrics").fetchall()
        columns = [item[0] for item in connection.description]
    finally:
        connection.close()

    assert rows == []
    assert columns == [
        "symbol",
        "period",
        "snapshot_time",
        "available_time",
        "sum_open_interest",
        "sum_open_interest_value",
        "count_toptrader_long_short_ratio",
        "sum_toptrader_long_short_ratio",
        "count_long_short_ratio",
        "sum_taker_long_short_vol_ratio",
        "source",
        "quality_status",
        "schema_version",
    ]
