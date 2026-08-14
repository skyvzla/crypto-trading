import hashlib
import json
import logging
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
from io import BytesIO
from threading import Barrier, Event
from types import SimpleNamespace
from unittest.mock import MagicMock

import duckdb
import httpx
import pytest

from trading_platform.backtest.loader import BacktestDataLoader
from trading_platform.market.archive import vision as archive_vision
from trading_platform.market.archive import (
    BinanceVisionHTTPFetcher,
    BinanceVisionWorkerPoolFetcher,
    Candle,
    DownloadProgress,
    DownloadResult,
    ParquetCandleArchive,
    aggtrade_archive_url,
    create_duckdb_catalog,
    download_history,
    kline_archive_url,
    monthly_aggtrade_archive_url,
    parse_aggtrade_archive,
    parse_kline_archive,
    parse_monthly_aggtrade_archive,
)
from trading_platform.market.archive import cli as archive_cli
from trading_platform.market.archive.cli import (
    _DiskSpaceGuard,
    _ProgressReporter,
    _load_allowed_symbols,
    _print_result,
    _require_index_for_existing_archive,
)


def test_archive_urls_default_to_binance_s3_origin():
    expected_root = (
        "https://s3-ap-northeast-1.amazonaws.com/"
        "data.binance.vision/data/futures/um"
    )

    assert aggtrade_archive_url("akeusdt", "2026-07-01") == (
        f"{expected_root}/daily/aggTrades/AKEUSDT/"
        "AKEUSDT-aggTrades-2026-07-01.zip"
    )
    assert kline_archive_url("akeusdt", "1m", "2026-07") == (
        f"{expected_root}/monthly/klines/AKEUSDT/1m/AKEUSDT-1m-2026-07.zip"
    )
    assert monthly_aggtrade_archive_url("akeusdt", "2026-07") == (
        f"{expected_root}/monthly/aggTrades/AKEUSDT/"
        "AKEUSDT-aggTrades-2026-07.zip"
    )


def test_existing_archive_requires_index_before_normal_download(tmp_path):
    root = tmp_path / "candles"
    (root / "BTCUSDT").mkdir(parents=True)

    with pytest.raises(RuntimeError, match="run market-archive-index"):
        _require_index_for_existing_archive(
            root, "archive_index.parquet", dataset_label="candles"
        )

    (root / "archive_index.parquet").touch()
    _require_index_for_existing_archive(
        root, "archive_index.parquet", dataset_label="candles"
    )


def test_empty_archive_does_not_require_index(tmp_path):
    root = tmp_path / "candles"
    root.mkdir()

    _require_index_for_existing_archive(
        root, "archive_index.parquet", dataset_label="candles"
    )


def test_exchange_info_parses_onboard_and_delivery_dates_in_utc():
    payload = {
        "symbols": [
            {
                "symbol": "BTWUSDT",
                "onboardDate": 1_780_272_000_000,
                "deliveryDate": 1_784_073_600_000,
            }
        ]
    }

    bounds = archive_vision.parse_symbol_availability(payload, ["BTWUSDT"])

    assert bounds["BTWUSDT"] == archive_vision.SymbolAvailability(
        onboard_time=datetime(2026, 6, 1, tzinfo=UTC),
        delivery_time=datetime(2026, 7, 15, tzinfo=UTC),
    )


def test_exchange_info_fetcher_requests_usdm_metadata():
    requested: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request)
        return httpx.Response(
            200,
            json={
                "symbols": [
                    {
                        "symbol": "BTWUSDT",
                        "onboardDate": 1_780_582_500_000,
                        "deliveryDate": 4_133_404_800_000,
                    }
                ]
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        bounds = archive_vision.BinanceFuturesMetadataFetcher(
            client,
            attempts=1,
        )(["btwusdt"])

    assert [str(request.url) for request in requested] == [
        "https://fapi.binance.com/fapi/v1/exchangeInfo"
    ]
    assert bounds["BTWUSDT"] == archive_vision.SymbolAvailability(
        onboard_time=datetime(2026, 6, 4, 14, 15, tzinfo=UTC),
        delivery_time=datetime(2100, 12, 25, 8, tzinfo=UTC),
    )


def test_symbol_availability_filters_pre_listing_and_post_delivery_periods():
    bounds = archive_vision.SymbolAvailability(
        onboard_time=datetime(2026, 6, 1, tzinfo=UTC),
        delivery_time=datetime(2026, 7, 15, tzinfo=UTC),
    )

    assert bounds.intersects(
        datetime(2026, 5, 1, tzinfo=UTC),
        datetime(2026, 6, 1, tzinfo=UTC),
    ) is False
    assert bounds.intersects(
        datetime(2026, 6, 1, tzinfo=UTC),
        datetime(2026, 7, 1, tzinfo=UTC),
    ) is True
    assert bounds.intersects(
        datetime(2026, 7, 1, tzinfo=UTC),
        datetime(2026, 8, 1, tzinfo=UTC),
    ) is True
    assert bounds.intersects(
        datetime(2026, 7, 15, tzinfo=UTC),
        datetime(2026, 7, 16, tzinfo=UTC),
    ) is False


def test_download_history_does_not_request_outside_symbol_lifecycle(tmp_path):
    payload = BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr(
            "BTWUSDT-aggTrades-2026-06-01.csv",
            "agg_trade_id,price,quantity,first_trade_id,last_trade_id,"
            "transact_time,is_buyer_maker\n"
            "1,1.0,2.0,1,1,1780272000100,false\n",
        )
    requested: list[str] = []

    def fetch(url: str) -> bytes:
        requested.append(url)
        assert "2026-06-01" in url
        return payload.getvalue()

    with ParquetCandleArchive(tmp_path / "history") as archive:
        results = download_history(
            archive,
            fetch=fetch,
            symbols=["BTWUSDT"],
            timeframes=["1s"],
            start=datetime(2026, 5, 31, tzinfo=UTC),
            end=datetime(2026, 6, 3, tzinfo=UTC),
            symbol_availability={
                "BTWUSDT": archive_vision.SymbolAvailability(
                    onboard_time=datetime(2026, 6, 1, tzinfo=UTC),
                    delivery_time=datetime(2026, 6, 2, tzinfo=UTC),
                )
            },
        )

    assert len(requested) == 1
    assert [(item.period, item.rows) for item in results] == [
        ("2026-06-01", 1)
    ]


def test_download_history_keeps_intersecting_listing_and_delivery_months():
    requested: list[str] = []

    def fetch(url: str) -> bytes:
        requested.append(url)
        month = url.removesuffix(".zip").rsplit("-", 2)[-2:]
        label = "-".join(month)
        payload = BytesIO()
        with zipfile.ZipFile(payload, "w") as archive:
            archive.writestr(
                f"BTWUSDT-1m-{label}.csv",
                "open_time,open,high,low,close,volume,close_time\n",
            )
        return payload.getvalue()

    class Archive:
        @staticmethod
        def upsert(candles):
            return len(candles)

    results = download_history(
        Archive(),
        fetch=fetch,
        symbols=["BTWUSDT"],
        timeframes=["1m"],
        start=datetime(2026, 5, 1, tzinfo=UTC),
        end=datetime(2026, 9, 1, tzinfo=UTC),
        symbol_availability={
            "BTWUSDT": archive_vision.SymbolAvailability(
                onboard_time=datetime(2026, 6, 15, tzinfo=UTC),
                delivery_time=datetime(2026, 7, 15, tzinfo=UTC),
            )
        },
    )

    assert [item.period for item in results] == ["2026-06", "2026-07"]
    assert [url.rsplit("/", 1)[-1] for url in requested] == [
        "BTWUSDT-1m-2026-06.zip",
        "BTWUSDT-1m-2026-07.zip",
    ]


@pytest.mark.parametrize(
    ("timeframe", "start", "end", "onboard_time", "delivery_time"),
    [
        (
            "1s",
            datetime(2026, 6, 4, tzinfo=UTC),
            datetime(2026, 6, 4, 12, tzinfo=UTC),
            datetime(2026, 6, 4, 14, 15, tzinfo=UTC),
            datetime(2100, 12, 25, 8, tzinfo=UTC),
        ),
        (
            "1m",
            datetime(2026, 6, 1, tzinfo=UTC),
            datetime(2026, 6, 2, tzinfo=UTC),
            datetime(2026, 6, 4, 14, 15, tzinfo=UTC),
            datetime(2100, 12, 25, 8, tzinfo=UTC),
        ),
        (
            "1s",
            datetime(2026, 7, 15, 16, tzinfo=UTC),
            datetime(2026, 7, 15, 18, tzinfo=UTC),
            datetime(2026, 6, 1, tzinfo=UTC),
            datetime(2026, 7, 15, 14, tzinfo=UTC),
        ),
        (
            "1m",
            datetime(2026, 7, 20, tzinfo=UTC),
            datetime(2026, 7, 21, tzinfo=UTC),
            datetime(2026, 6, 1, tzinfo=UTC),
            datetime(2026, 7, 15, 14, tzinfo=UTC),
        ),
    ],
)
def test_download_history_skips_partition_outside_requested_lifecycle_slice(
    timeframe,
    start,
    end,
    onboard_time,
    delivery_time,
):
    def reject_fetch(url: str) -> bytes:
        raise AssertionError(f"out-of-lifecycle partition was requested: {url}")

    class Archive:
        @staticmethod
        def upsert(candles):
            return len(candles)

    results = download_history(
        Archive(),
        fetch=reject_fetch,
        symbols=["BTWUSDT"],
        timeframes=[timeframe],
        start=start,
        end=end,
        symbol_availability={
            "BTWUSDT": archive_vision.SymbolAvailability(
                onboard_time=onboard_time,
                delivery_time=delivery_time,
            )
        },
    )

    assert results == []


def test_aggtrade_archive_aggregates_millisecond_and_microsecond_timestamps():
    payload = BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr(
            "AKEUSDT-aggTrades-2026-07-01.csv",
            "agg_trade_id,price,quantity,first_trade_id,last_trade_id,"
            "transact_time,is_buyer_maker\n"
            "2,12,3,2,2,1782864000900000,true\n"
            "1,10,2,1,1,1782864000100,false\n"
            "3,11,5,3,3,1782864001100,false\n",
        )

    candles = parse_aggtrade_archive(
        payload.getvalue(), "AKEUSDT", "2026-07-01"
    )

    assert [(item.open, item.high, item.low, item.close, item.volume) for item in candles] == [
        (10.0, 12.0, 10.0, 12.0, 5.0),
        (11.0, 11.0, 11.0, 11.0, 5.0),
    ]
    assert candles[0].open_time == datetime(2026, 7, 1, tzinfo=UTC)


def test_monthly_aggtrade_archive_streams_daily_candle_partitions():
    payload = BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr(
            "AKEUSDT-aggTrades-2026-07.csv",
            "agg_trade_id,price,quantity,first_trade_id,last_trade_id,"
            "transact_time,is_buyer_maker\n"
            "1,10,2,1,1,1782864000100,false\n"
            "2,12,3,2,2,1782864000900,true\n"
            "3,11,5,3,3,1782950400100,false\n",
        )

    partitions = list(
        parse_monthly_aggtrade_archive(
            payload.getvalue(), "AKEUSDT", "2026-07"
        )
    )

    assert [day.isoformat() for day, _candles in partitions] == [
        "2026-07-01",
        "2026-07-02",
    ]
    assert [
        (candle.open, candle.high, candle.low, candle.close, candle.volume)
        for _day, candles in partitions
        for candle in candles
    ] == [
        (10.0, 12.0, 10.0, 12.0, 5.0),
        (11.0, 11.0, 11.0, 11.0, 5.0),
    ]


def test_download_history_uses_one_monthly_aggtrade_archive_for_complete_month(
    tmp_path,
):
    payload = BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr(
            "AKEUSDT-aggTrades-2026-07.csv",
            "agg_trade_id,price,quantity,first_trade_id,last_trade_id,"
            "transact_time,is_buyer_maker\n"
            "1,10,2,1,1,1782864000100,false\n"
            "2,11,3,2,2,1782950400100,false\n",
        )
    requested: list[str] = []

    def fetch(url: str) -> bytes:
        requested.append(url)
        return payload.getvalue()

    archive_root = tmp_path / "history"
    with ParquetCandleArchive(archive_root) as archive:
        results = download_history(
            archive,
            fetch=fetch,
            symbols=["AKEUSDT"],
            timeframes=["1s"],
            start=datetime(2026, 7, 1, tzinfo=UTC),
            end=datetime(2026, 8, 1, tzinfo=UTC),
        )

    assert [url.rsplit("/", 1)[-1] for url in requested] == [
        "AKEUSDT-aggTrades-2026-07.zip"
    ]
    assert "/monthly/aggTrades/" in requested[0]
    assert [(item.period, item.rows) for item in results] == [("2026-07", 2)]
    assert (archive_root / "AKEUSDT/1s/2026/07/01/candles.parquet").is_file()
    assert (archive_root / "AKEUSDT/1s/2026/07/02/candles.parquet").is_file()


def test_monthly_aggtrade_download_uses_arrow_table_upsert():
    payload = BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr(
            "AKEUSDT-aggTrades-2026-07.csv",
            "agg_trade_id,price,quantity,first_trade_id,last_trade_id,"
            "transact_time,is_buyer_maker\n"
            "1,10,2,1,1,1782864000100,false\n"
            "2,12,3,2,2,1782864000900000,true\n",
        )

    tables = []

    class ArrowArchive:
        @staticmethod
        def partition_rows(*_args):
            return None

        @staticmethod
        def upsert(_candles):
            pytest.fail("monthly 1s data must use Arrow table upsert")

        @staticmethod
        def upsert_table(table, **partition):
            tables.append((table, partition))
            return table.num_rows

    results = download_history(
        ArrowArchive(),
        fetch=lambda _url: payload.getvalue(),
        symbols=["AKEUSDT"],
        timeframes=["1s"],
        start=datetime(2026, 7, 1, tzinfo=UTC),
        end=datetime(2026, 8, 1, tzinfo=UTC),
    )

    assert results[0].rows == 1
    assert len(tables) == 1
    table, partition = tables[0]
    assert table.column_names == [
        "symbol",
        "timeframe",
        "open_time",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "close_time",
    ]
    assert table.to_pylist()[0]["close"] == 12.0
    assert partition == {
        "symbol": "AKEUSDT",
        "timeframe": "1s",
        "year": 2026,
        "month": 7,
        "day": 1,
    }


def test_monthly_aggtrade_download_resumes_without_replacing_existing_days(
    tmp_path,
):
    archive_root = tmp_path / "history"
    existing = Candle(
        symbol="AKEUSDT",
        timeframe="1s",
        open_time=datetime(2026, 7, 1, tzinfo=UTC),
        open=99,
        high=99,
        low=99,
        close=99,
        volume=1,
        close_time=datetime(2026, 7, 1, 0, 0, 1, tzinfo=UTC),
    )
    with ParquetCandleArchive(archive_root) as archive:
        archive.upsert([existing])

    payload = BytesIO()
    with zipfile.ZipFile(payload, "w") as source:
        source.writestr(
            "AKEUSDT-aggTrades-2026-07.csv",
            "agg_trade_id,price,quantity,first_trade_id,last_trade_id,"
            "transact_time,is_buyer_maker\n"
            "1,10,2,1,1,1782864000100,false\n"
            "2,11,3,2,2,1782950400100,false\n",
        )

    with ParquetCandleArchive(archive_root) as archive:
        download_history(
            archive,
            fetch=lambda _url: payload.getvalue(),
            symbols=["AKEUSDT"],
            timeframes=["1s"],
            start=datetime(2026, 7, 1, tzinfo=UTC),
            end=datetime(2026, 8, 1, tzinfo=UTC),
        )

    catalog = create_duckdb_catalog(archive_root, tmp_path / "history.duckdb")
    connection = duckdb.connect(str(catalog), read_only=True)
    try:
        rows = connection.execute(
            "SELECT day(open_time), open FROM candles ORDER BY open_time"
        ).fetchall()
    finally:
        connection.close()
    assert rows == [(1, 99.0), (2, 11.0)]


def test_download_history_keeps_partial_month_seconds_on_daily_archives(tmp_path):
    requested: list[str] = []

    def fetch(url: str) -> bytes:
        requested.append(url)
        label = url.removesuffix(".zip").rsplit("aggTrades-", 1)[1]
        payload = BytesIO()
        with zipfile.ZipFile(payload, "w") as archive:
            archive.writestr(
                f"AKEUSDT-aggTrades-{label}.csv",
                "agg_trade_id,price,quantity,first_trade_id,last_trade_id,"
                "transact_time,is_buyer_maker\n",
            )
        return payload.getvalue()

    with ParquetCandleArchive(tmp_path / "history") as archive:
        download_history(
            archive,
            fetch=fetch,
            symbols=["AKEUSDT"],
            timeframes=["1s"],
            start=datetime(2026, 7, 30, tzinfo=UTC),
            end=datetime(2026, 8, 2, tzinfo=UTC),
        )

    assert len(requested) == 3
    assert all("/daily/aggTrades/" in url for url in requested)


def test_kline_archive_parses_epoch_without_session_timezone_conversion():
    payload = BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr(
            "AKEUSDT-1m-2026-07.csv",
            "1782864000000,10,12,9,11,20,1782864059999,0,0,0,0,0\n",
        )

    candles = parse_kline_archive(
        payload.getvalue(), "AKEUSDT", "1m", "2026-07"
    )

    assert len(candles) == 1
    assert candles[0].open_time == datetime(2026, 7, 1, tzinfo=UTC)
    assert candles[0].close_time == datetime(
        2026, 7, 1, 0, 0, 59, 999000, tzinfo=UTC
    )


def test_download_history_imports_daily_seconds_and_monthly_klines(tmp_path):
    aggtrade_payload = BytesIO()
    with zipfile.ZipFile(aggtrade_payload, "w") as archive:
        archive.writestr(
            "AKEUSDT-aggTrades-2026-07-01.csv",
            "agg_trade_id,price,quantity,first_trade_id,last_trade_id,"
            "transact_time,is_buyer_maker\n"
            "1,10,2,1,1,1782864000100,false\n",
        )
    kline_payload = BytesIO()
    with zipfile.ZipFile(kline_payload, "w") as archive:
        archive.writestr(
            "AKEUSDT-1m-2026-07.csv",
            "1782864000000,10,10,10,10,2,1782864059999,0,0,0,0,0\n"
            "1783036800000,11,11,11,11,3,1783036859999,0,0,0,0,0\n",
        )

    def fetch(url: str) -> bytes:
        if "/daily/aggTrades/" in url:
            return aggtrade_payload.getvalue()
        if "/monthly/klines/" in url:
            return kline_payload.getvalue()
        raise AssertionError(url)

    archive_root = tmp_path / "history"
    progress: list[DownloadProgress] = []
    with ParquetCandleArchive(archive_root) as archive:
        results = download_history(
            archive,
            fetch=fetch,
            symbols=["akeusdt"],
            timeframes=["1s", "1m"],
            start=datetime(2026, 7, 1, tzinfo=UTC),
            end=datetime(2026, 7, 2, tzinfo=UTC),
            on_progress=progress.append,
            max_workers=2,
        )

    assert {(item.timeframe, item.rows) for item in results} == {
        ("1s", 1),
        ("1m", 2),
    }
    assert sorted(
        (item.current, item.total, item.phase) for item in progress
    ) == sorted([
        (1, 2, "downloading"),
        (1, 2, "downloaded"),
        (1, 2, "processing"),
        (1, 2, "stored"),
        (2, 2, "downloading"),
        (2, 2, "downloaded"),
        (2, 2, "processing"),
        (2, 2, "stored"),
    ])
    assert (archive_root / "AKEUSDT/1s/2026/07/01/candles.parquet").is_file()
    assert (archive_root / "AKEUSDT/1m/2026/07/00/candles.parquet").is_file()
    assert all(
        "=" not in part
        for path in archive_root.rglob("*.parquet")
        for part in path.parts
    )
    temporary = archive_root / "AKEUSDT/1s/2026/07/01/.candles-stale.tmp.parquet"
    temporary.write_bytes(
        (archive_root / "AKEUSDT/1s/2026/07/01/candles.parquet").read_bytes()
    )
    skipped_progress: list[DownloadProgress] = []

    def reject_fetch(url: str) -> bytes:
        raise AssertionError(f"existing partition was downloaded again: {url}")

    with ParquetCandleArchive(archive_root) as archive:
        repeated = download_history(
            archive,
            fetch=reject_fetch,
            symbols=["AKEUSDT"],
            timeframes=["1s", "1m"],
            start=datetime(2026, 7, 1, tzinfo=UTC),
            end=datetime(2026, 7, 2, tzinfo=UTC),
            on_progress=skipped_progress.append,
            max_workers=2,
        )

    assert [item.skipped for item in repeated] == [True, True]
    assert [item.phase for item in skipped_progress] == ["skipped", "skipped"]
    catalog = create_duckdb_catalog(archive_root, tmp_path / "history.duckdb")
    connection = duckdb.connect(str(catalog), read_only=True)
    try:
        connection.execute("SET TimeZone = 'Asia/Shanghai'")
        stored = connection.execute(
            """
            SELECT timeframe, epoch_ms(open_time), epoch_ms(close_time)
            FROM candles
            ORDER BY timeframe, open_time
            """
        ).fetchall()
    finally:
        connection.close()
    assert stored == [
        ("1m", 1_782_864_000_000, 1_782_864_059_999),
        ("1m", 1_783_036_800_000, 1_783_036_859_999),
        ("1s", 1_782_864_000_000, 1_782_864_001_000),
    ]
    events = BacktestDataLoader(
        symbols=["AKEUSDT"],
        start_ms=1_782_864_000_000,
        end_ms=1_782_864_060_000,
        require_aggtrades=True,
        required_kline_intervals=["1m"],
        duckdb_path=str(catalog),
    ).iter_all()
    events = list(events)
    assert [event.timestamp for event in events if hasattr(event, "timestamp")] == [
        1_782_864_000_000
    ]


def test_http_fetcher_verifies_binance_checksum():
    content = b"verified archive"
    checksum = hashlib.sha256(content).hexdigest()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(".CHECKSUM"):
            return httpx.Response(200, text=f"{checksum}  archive.zip\n")
        return httpx.Response(200, content=content)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        fetch = BinanceVisionHTTPFetcher(client, attempts=1)
        result = fetch("https://data.binance.vision/archive.zip")

    assert result == content


def test_http_fetcher_exposes_verified_seekable_stream():
    content = b"verified streamed archive"
    checksum = hashlib.sha256(content).hexdigest()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(".CHECKSUM"):
            return httpx.Response(200, text=checksum)
        return httpx.Response(200, content=content)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        fetch = BinanceVisionHTTPFetcher(client, attempts=1)
        with fetch.open_archive("https://data.binance.vision/archive.zip") as source:
            assert source.read() == content
            source.seek(0)
            assert source.read(8) == b"verified"


def test_worker_pool_switches_proxy_after_connection_failure():
    calls: list[str] = []
    retries: list[tuple[int, str | None]] = []
    routes: list[tuple[str, str | None, str, str | None]] = []

    class Fetcher:
        def __init__(self, name: str) -> None:
            self.name = name

        def __call__(self, _url: str) -> bytes:
            calls.append(self.name)
            if self.name == "a":
                raise httpx.ConnectError("connection reset")
            return self.name.encode()

    pool = BinanceVisionWorkerPoolFetcher(
        [Fetcher("a"), Fetcher("b")],
        direct_fetcher=Fetcher("direct"),
        attempts=5,
        labels=["proxy-a", "proxy-b"],
        on_retry=lambda _url, attempt, _attempts, _error, proxy=None, **_kwargs: (
            retries.append((attempt, proxy))
        ),
        on_route=lambda _url, _attempt, _attempts, mode, source,
        previous_source=None, reason=None, **_kwargs: routes.append(
            (mode, previous_source, source, reason)
        ),
    )

    assert pool("archive") == b"b"
    assert calls == ["a", "b"]
    assert retries == [(2, "proxy-a")]
    assert routes == [("switch", "proxy-a", "proxy-b", None)]


def test_worker_pool_releases_proxy_when_route_logger_fails():
    route_fails = True

    def route(*_args, **_kwargs) -> None:
        nonlocal route_fails
        if route_fails:
            route_fails = False
            raise RuntimeError("route logger failed")

    def fail_proxy_a(_url: str) -> bytes:
        raise httpx.ConnectError("proxy-a reset")

    pool = BinanceVisionWorkerPoolFetcher(
        [fail_proxy_a, lambda _url: b"proxy-b"],
        direct_fetcher=lambda _url: b"direct",
        attempts=5,
        retry_base_seconds=0,
        on_route=route,
    )

    with pytest.raises(RuntimeError, match="route logger failed"):
        pool("first")
    assert pool("second") == b"proxy-b"


def test_worker_pool_switches_proxy_for_streaming_archives():
    calls: list[str] = []

    class Fetcher:
        def __init__(self, name: str, *, fails: bool = False) -> None:
            self.name = name
            self.fails = fails

        @contextmanager
        def open_archive(self, _url: str):
            calls.append(self.name)
            if self.fails:
                raise httpx.ConnectError("connection reset")
            yield BytesIO(self.name.encode())

    pool = BinanceVisionWorkerPoolFetcher(
        [Fetcher("a", fails=True), Fetcher("b")],
        direct_fetcher=Fetcher("direct"),
        attempts=5,
        labels=["proxy-a", "proxy-b"],
        retry_base_seconds=0,
    )

    with pool.open_archive("archive") as source:
        assert source.read() == b"b"
    assert calls == ["a", "b"]


def test_worker_pool_uses_direct_when_all_proxies_are_occupied():
    started = Event()
    release = Event()

    class Fetcher:
        def __call__(self, _url: str) -> bytes:
            started.set()
            release.wait(timeout=1)
            return b"proxy"

    routes: list[tuple[str, str | None]] = []
    pool = BinanceVisionWorkerPoolFetcher(
        [Fetcher()],
        direct_fetcher=lambda _url: b"direct",
        attempts=5,
        labels=["proxy-a"],
        on_route=lambda _url, _attempt, _attempts, _mode, _source,
        previous_source=None, reason=None, **_kwargs: routes.append(
            (previous_source, reason)
        ),
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        occupied = executor.submit(pool, "first")
        assert started.wait(timeout=1)
        fallback = executor.submit(pool, "second")
        assert fallback.result(timeout=1) == b"direct"
        release.set()
        assert occupied.result(timeout=1) == b"proxy"
    assert routes == [(None, "no-available-proxy")]


def test_worker_pool_reserves_final_attempt_for_direct_access():
    calls: list[str] = []
    routes: list[tuple[int, str | None]] = []

    def failing_proxy(url: str) -> bytes:
        calls.append(url)
        raise httpx.ConnectError("connection reset")

    pool = BinanceVisionWorkerPoolFetcher(
        [failing_proxy] * 8,
        direct_fetcher=lambda _url: b"direct",
        attempts=5,
        retry_base_seconds=0,
        on_route=lambda _url, attempt, _attempts, _mode, _source,
        reason=None, **_kwargs: routes.append((attempt, reason)),
    )

    assert pool("archive") == b"direct"
    assert calls == ["archive"] * 4
    assert routes[-1] == (5, "final-attempt")


def test_worker_pool_reports_final_direct_failure():
    calls: list[str] = []
    routes: list[tuple[str | None, str, str | None]] = []

    def fail(source: str):
        def fetch(_url: str) -> bytes:
            calls.append(source)
            raise httpx.ConnectError(f"{source} reset")

        return fetch

    pool = BinanceVisionWorkerPoolFetcher(
        [fail("proxy-a"), fail("proxy-b")],
        direct_fetcher=fail("direct"),
        attempts=5,
        retry_base_seconds=0,
        on_route=lambda _url, _attempt, _attempts, _mode, source,
        previous_source=None, reason=None, **_kwargs: routes.append(
            (previous_source, source, reason)
        ),
    )

    with pytest.raises(RuntimeError, match=r"source=direct after 5 attempts"):
        pool("archive")
    assert calls == ["proxy-a", "proxy-b", "direct", "direct", "direct"]
    assert routes == [
        ("proxy-1", "proxy-2", None),
        ("proxy-2", "direct", "no-available-proxy"),
    ]


def test_cli_progress_uses_monotonic_completion_count(capsys):
    reporter = _ProgressReporter(workers=4)
    reporter.retry(
        "https://example.com/BTWUSDT-aggTrades-2026-06-04.zip",
        2,
        3,
        httpx.ConnectError("temporary outage"),
        proxy="socks5h://proxy-a:1080",
        elapsed_seconds=10,
        worker_id=1,
    )
    reporter.route(
        "https://example.com/BTWUSDT-aggTrades-2026-06-04.zip",
        2,
        3,
        "switch",
        "socks5h://proxy-b:1080",
        previous_source="socks5h://proxy-a:1080",
        worker_id=1,
    )
    reporter.worker_exit(1)
    reporter.route(
        "https://example.com/BTWUSDT-aggTrades-2026-06-04.zip",
        3,
        3,
        "fallback",
        "direct",
        previous_source="socks5h://proxy-b:1080",
        reason="final-attempt",
        worker_id=1,
    )
    reporter(
        DownloadProgress(
            phase="downloaded",
            worker_id=1,
            downloaded_bytes=2 * 1024 * 1024,
            elapsed_seconds=1,
            current=68,
            total=68,
            symbol="BANKUSDT",
            timeframe="15m",
            period="2026-07",
        )
    )
    reporter(
        DownloadProgress(
            phase="stored",
            worker_id=1,
            current=68,
            total=68,
            symbol="BANKUSDT",
            timeframe="15m",
            period="2026-07",
            elapsed_seconds=5,
            download_seconds=1,
            processing_seconds=4.4,
            rows=2976,
        )
    )
    reporter(
        DownloadProgress(
            phase="skipped",
            worker_id=2,
            current=64,
            total=68,
            symbol="BANKUSDT",
            timeframe="1s",
            period="2026-07-30",
            rows=86249,
        )
    )
    reporter(
        DownloadProgress(
            phase="failed",
            worker_id=3,
            current=65,
            total=68,
            symbol="BANKUSDT",
            timeframe="1s",
            period="2026-07-31",
            elapsed_seconds=65,
            error="ConnectError: connection reset",
        )
    )
    reporter.close()

    output = capsys.readouterr().err
    assert "event=start task=market-archive total=68" in output
    assert "event=complete task=market-archive status=ok done=3" in output
    assert "event=error" not in output
    assert "\x1b[" not in output
    assert "worker=1 [1/68] BANKUSDT 15m 2026-07 stored 2976 rows" not in output


def test_cli_progress_starts_on_download_and_records_early_terminal_states():
    reporter = _ProgressReporter(workers=4)
    reporter(
        DownloadProgress(
            phase="downloading",
            worker_id=1,
            current=1,
            total=4,
            symbol="BTCUSDT",
            timeframe="1m",
            period="2026-08",
        )
    )

    dashboard = reporter._dashboard
    assert dashboard is not None
    assert set(dashboard._running) == {"w1 BTCUSDT 1m 2026-08"}

    for phase, worker_id, symbol in [
        ("skipped", 2, "ETHUSDT"),
        ("unavailable", 3, "SOLUSDT"),
        ("failed", 4, "XRPUSDT"),
    ]:
        reporter(
            DownloadProgress(
                phase=phase,
                worker_id=worker_id,
                current=worker_id,
                total=4,
                symbol=symbol,
                timeframe="1m",
                period="2026-08",
                error="connection reset" if phase == "failed" else "",
            )
        )

    assert [
        (item.name, item.status) for item in dashboard._completed
    ] == [
        ("w4 XRPUSDT 1m 2026-08", "Failed"),
        ("w3 SOLUSDT 1m 2026-08", "Unavailable"),
        ("w2 ETHUSDT 1m 2026-08", "Skipped"),
    ]
    reporter.close()


def test_download_history_assigns_process_local_worker_sequence_numbers():
    barrier = Barrier(2)
    progress: list[DownloadProgress] = []
    exited_workers: list[int] = []

    class ExistingArchive:
        def partition_rows(self, *_args) -> int:
            barrier.wait(timeout=1)
            return 1

    results = download_history(
        ExistingArchive(),
        fetch=lambda _url: pytest.fail("existing partitions must not download"),
        symbols=["AKEUSDT", "BTCUSDT"],
        timeframes=["1m"],
        start=datetime(2026, 7, 1, tzinfo=UTC),
        end=datetime(2026, 8, 1, tzinfo=UTC),
        on_progress=progress.append,
        max_workers=2,
        on_worker_exit=exited_workers.append,
    )

    assert all(result.skipped for result in results)
    assert {item.worker_id for item in progress} == {1, 2}
    assert exited_workers == [1, 2]


def test_cli_result_is_plain_text_by_default(tmp_path, capsys):
    results = [DownloadResult("AKEUSDT", "1m", "2026-07", 44640)]

    _print_result(
        results,
        tmp_path / "parquet",
        tmp_path / "history.duckdb",
        as_json=False,
    )

    output = capsys.readouterr().out
    assert output.startswith(
        "Complete: 1 downloaded, 0 existing, 0 unavailable, 44640 rows."
    )
    assert not output.lstrip().startswith("{")


def test_load_allowed_symbols_uses_exchange_lifecycle_gate(monkeypatch):
    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.fetchall.return_value = [("AKEUSDT",), ("BTCUSDT",)]
    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection.cursor.return_value = cursor
    connect = MagicMock(return_value=connection)
    monkeypatch.setattr(archive_cli.psycopg, "connect", connect)

    symbols = _load_allowed_symbols("postgresql://archive", freeze_days=15)

    assert symbols == ["AKEUSDT", "BTCUSDT"]
    connect.assert_called_once_with("postgresql://archive")
    query = cursor.execute.call_args.args[0]
    assert "active = TRUE" in query
    assert "contract_type = 'PERPETUAL'" in query
    assert "status = 'TRADING'" in query
    assert "onboard_date <= NOW()" in query
    assert "delivery_date > NOW() + %s" in query
    assert cursor.execute.call_args.args[1] == (
        archive_cli.timedelta(days=15),
        None,
        None,
    )


def test_disk_space_guard_stops_at_configured_reserve(tmp_path, monkeypatch):
    monkeypatch.setattr(
        archive_cli.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(free=10 * 1024**3),
    )

    guard = _DiskSpaceGuard(tmp_path / "not-created-yet", min_free_gb=10)

    with pytest.raises(RuntimeError, match="insufficient disk space"):
        guard()


def test_cli_without_symbols_loads_all_tradable_symbols(
    tmp_path, monkeypatch, capsys
):
    loaded = MagicMock(return_value=["AKEUSDT", "BTCUSDT"])
    captured: dict[str, object] = {}

    def fake_download(_archive, **kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(archive_cli, "_load_allowed_symbols", loaded)
    monkeypatch.setattr(
        archive_cli,
        "BinanceFuturesMetadataFetcher",
        lambda *args, **kwargs: lambda symbols: {},
    )
    monkeypatch.setattr(archive_cli, "download_history", fake_download)
    monkeypatch.setattr(
        archive_cli,
        "ensure_duckdb_catalog",
        lambda _archive, catalog: catalog,
    )

    exit_code = archive_cli.main(
        [
            str(tmp_path / "parquet"),
            "--dsn",
            "postgresql://archive",
            "--timeframes",
            "1m",
            "--start",
            "2026-07-01T00:00:00Z",
            "--end",
            "2026-08-01T00:00:00Z",
            "--min-free-gb",
            "0",
            "--without-metrics",
        ]
    )

    assert exit_code == 0
    loaded.assert_called_once_with(
        "postgresql://archive", freeze_days=15, strategy_id=None
    )
    assert captured["symbols"] == ["AKEUSDT", "BTCUSDT"]
    stderr = capsys.readouterr().err
    assert "Loaded 2 tradable symbols from PostgreSQL." in stderr
    assert "Downloading data for 2 trading pairs." in stderr


def test_cli_uses_failover_proxy_pool(
    tmp_path, monkeypatch, capsys
):
    client_options: list[dict[str, object]] = []
    captured: dict[str, object] = {}

    class Client:
        def __init__(self, **kwargs) -> None:
            client_options.append(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *_exc) -> None:
            return None

    def fake_download(_archive, **kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(archive_cli.httpx, "Client", Client)
    monkeypatch.setattr(
        archive_cli,
        "BinanceFuturesMetadataFetcher",
        lambda *args, **kwargs: lambda symbols: {},
    )
    monkeypatch.setattr(archive_cli, "download_history", fake_download)
    monkeypatch.setattr(
        archive_cli,
        "ensure_duckdb_catalog",
        lambda _archive, catalog: catalog,
    )

    exit_code = archive_cli.main(
        [
            str(tmp_path / "parquet"),
            "--symbols",
            "AKEUSDT",
            "--timeframes",
            "1s",
            "--start",
            "2026-07-01T00:00:00Z",
            "--end",
            "2026-08-01T00:00:00Z",
            "--min-free-gb",
            "0",
            "--without-metrics",
            "--proxy",
            "http://proxy-a:8080",
            "--proxy",
            "http://proxy-b:8080",
        ]
    )

    assert exit_code == 0
    assert captured["max_workers"] == 2
    assert isinstance(captured["fetch"], BinanceVisionWorkerPoolFetcher)
    assert [options.get("proxy") for options in client_options] == [
        None,
        "http://proxy-a:8080",
        "http://proxy-b:8080",
    ]
    assert all(options["trust_env"] is False for options in client_options)
    assert (
        "Using 2 failover proxies with 2 workers and direct fallback."
        in capsys.readouterr().err
    )


def test_cli_accepts_socks5_proxy(tmp_path, monkeypatch):
    client_options: list[dict[str, object]] = []

    class Client:
        def __init__(self, **kwargs) -> None:
            client_options.append(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *_exc) -> None:
            return None

    monkeypatch.setattr(archive_cli.httpx, "Client", Client)
    monkeypatch.setattr(
        archive_cli,
        "BinanceFuturesMetadataFetcher",
        lambda *args, **kwargs: lambda symbols: {},
    )
    monkeypatch.setattr(archive_cli, "download_history", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        archive_cli,
        "ensure_duckdb_catalog",
        lambda _archive, catalog: catalog,
    )

    assert archive_cli.main(
        [
            str(tmp_path / "parquet"),
            "--symbols", "AKEUSDT",
            "--timeframes", "1m",
            "--start", "2026-07-01T00:00:00Z",
            "--end", "2026-08-01T00:00:00Z",
            "--min-free-gb", "0",
            "--without-metrics",
            "--proxy", "socks5://proxy-a:1080",
        ]
    ) == 0
    proxy_options = [options for options in client_options if "proxy" in options]
    assert proxy_options[0]["proxy"] == "socks5://proxy-a:1080"


def test_catalog_supports_an_all_unavailable_download(tmp_path):
    catalog = create_duckdb_catalog(
        tmp_path / "empty-parquet", tmp_path / "history.duckdb"
    )

    connection = duckdb.connect(str(catalog), read_only=True)
    try:
        rows = connection.execute("SELECT * FROM candles").fetchall()
        columns = [item[0] for item in connection.description]
    finally:
        connection.close()

    assert rows == []
    assert columns == [
        "symbol",
        "timeframe",
        "open_time",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "close_time",
    ]


def test_cli_handles_keyboard_interrupt_without_traceback(
    tmp_path, monkeypatch, capsys
):
    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(archive_cli, "download_history", interrupt)
    monkeypatch.setattr(
        archive_cli,
        "BinanceFuturesMetadataFetcher",
        lambda *args, **kwargs: lambda symbols: {},
    )

    exit_code = archive_cli.main(
        [
            str(tmp_path / "parquet"),
            "--symbols",
            "AKEUSDT",
            "--timeframes",
            "1s",
            "--start",
            "2026-07-01T00:00:00Z",
            "--end",
            "2026-07-02T00:00:00Z",
            "--without-metrics",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 130
    assert captured.out == ""
    assert captured.err == (
        "worker=main Downloading data for 1 trading pair.\n"
        "worker=main Cancelled; downloader exiting.\n"
    )
    assert "Traceback" not in captured.err


def test_cli_reports_default_log_setup_failure_as_json(tmp_path, monkeypatch, capsys):
    def fail_log_setup(*_args, **_kwargs):
        raise OSError("log directory is unavailable")

    monkeypatch.setattr(archive_cli, "_setup_logging", fail_log_setup)

    exit_code = archive_cli.main(
        [
            str(tmp_path / "parquet"),
            "--symbols",
            "AKEUSDT",
            "--timeframes",
            "1s",
            "--start",
            "2026-07-01T00:00:00Z",
            "--end",
            "2026-07-02T00:00:00Z",
            "--without-metrics",
            "--json",
        ]
    )

    assert exit_code == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "failed",
        "error": "log directory is unavailable",
    }


@pytest.mark.parametrize(
    (
        "outcome",
        "expected_exit",
        "expected_candle_status",
        "expected_metrics_status",
    ),
    [
        ("complete", 0, "ok", "ok"),
        ("failed", 1, "ok", "failed"),
        ("interrupted", 130, "ok", "interrupted"),
    ],
)
def test_cli_closes_metrics_reporter_for_every_exit_path(
    tmp_path,
    monkeypatch,
    outcome,
    expected_exit,
    expected_candle_status,
    expected_metrics_status,
):
    class Reporter:
        instances: list["Reporter"] = []

        def __init__(self, *_args, **_kwargs) -> None:
            self.statuses: list[str] = []
            self.instances.append(self)

        def close(self, *, status: str = "ok") -> None:
            self.statuses.append(status)

        def retry(self, *_args, **_kwargs) -> None:
            return None

        def metadata_fallback(self, *_args, **_kwargs) -> None:
            return None

        def worker_exit(self, *_args, **_kwargs) -> None:
            return None

    def fake_metrics_download(*_args, **_kwargs):
        assert Reporter.instances[0].statuses == ["ok"]
        if outcome == "failed":
            raise RuntimeError("metrics download failed")
        if outcome == "interrupted":
            raise KeyboardInterrupt
        return []

    monkeypatch.setattr(archive_cli, "_ProgressReporter", Reporter)
    monkeypatch.setattr(
        archive_cli,
        "BinanceFuturesMetadataFetcher",
        lambda *_args, **_kwargs: lambda _symbols: {},
    )
    monkeypatch.setattr(archive_cli, "download_history", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        archive_cli,
        "download_metrics_history",
        fake_metrics_download,
    )
    monkeypatch.setattr(
        archive_cli,
        "ensure_duckdb_catalog",
        lambda _archive, catalog: catalog,
    )
    monkeypatch.setattr(
        archive_cli.MetricsArchive,
        "publish",
        lambda _archive, catalog: (_archive.root / "metrics_index.parquet", catalog),
    )

    exit_code = archive_cli.main(
        [
            str(tmp_path / "candles"),
            "--symbols",
            "AKEUSDT",
            "--timeframes",
            "1m",
            "--start",
            "2026-07-01T00:00:00Z",
            "--end",
            "2026-07-02T00:00:00Z",
            "--min-free-gb",
            "0",
            "--log-file",
            str(tmp_path / "archive.log"),
        ]
    )

    assert exit_code == expected_exit
    assert [reporter.statuses for reporter in Reporter.instances] == [
        [expected_candle_status],
        [expected_metrics_status],
    ]


def test_setup_logging_replaces_archive_cli_handlers(tmp_path):
    archive_logger = logging.getLogger("trading_platform.market.archive")
    original_handlers = list(archive_logger.handlers)
    original_level = archive_logger.level
    archive_logger.handlers.clear()
    try:
        archive_cli._setup_logging("INFO", tmp_path / "first.log")
        archive_cli._setup_logging("INFO", tmp_path / "second.log")

        assert len(archive_logger.handlers) == 2
    finally:
        for handler in list(archive_logger.handlers):
            archive_logger.removeHandler(handler)
            handler.close()
        archive_logger.handlers.extend(original_handlers)
        archive_logger.setLevel(original_level)


def test_http_fetcher_falls_back_to_binance_s3_origin():
    content = b"origin archive"
    checksum = hashlib.sha256(content).hexdigest()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "data.binance.vision":
            raise httpx.ConnectError("public endpoint unavailable", request=request)
        assert request.url.host == "s3-ap-northeast-1.amazonaws.com"
        if request.url.path.endswith(".CHECKSUM"):
            return httpx.Response(200, text=checksum)
        return httpx.Response(200, content=content)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = BinanceVisionHTTPFetcher(client, attempts=1)(
            "https://data.binance.vision/data/archive.zip"
        )

    assert result == content


def test_http_fetcher_falls_back_to_public_endpoint_from_s3():
    content = b"public archive"
    checksum = hashlib.sha256(content).hexdigest()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "s3-ap-northeast-1.amazonaws.com":
            raise httpx.ConnectError("S3 unavailable", request=request)
        assert request.url.host == "data.binance.vision"
        if request.url.path.endswith(".CHECKSUM"):
            return httpx.Response(200, text=checksum)
        return httpx.Response(200, content=content)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = BinanceVisionHTTPFetcher(client, attempts=1)(
            "https://s3-ap-northeast-1.amazonaws.com/"
            "data.binance.vision/data/archive.zip"
        )

    assert result == content


def test_http_fetcher_does_not_hide_network_error_behind_other_origin_404():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "s3-ap-northeast-1.amazonaws.com":
            return httpx.Response(404, request=request)
        raise httpx.ConnectError("public endpoint reset", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        fetcher = BinanceVisionHTTPFetcher(client, attempts=1)
        with pytest.raises(httpx.ConnectError, match="public endpoint reset"):
            fetcher(
                "https://s3-ap-northeast-1.amazonaws.com/"
                "data.binance.vision/data/archive.zip"
            )


def test_download_history_skips_pre_listing_404_and_continues(tmp_path):
    payload = BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr(
            "BTWUSDT-aggTrades-2026-06-01.csv",
            "agg_trade_id,price,quantity,first_trade_id,last_trade_id,"
            "transact_time,is_buyer_maker\n"
            "1,1.0,2.0,1,1,1780272000100,false\n",
        )

    def fetch(url: str) -> bytes:
        if "2026-05-31" in url:
            raise archive_vision.ArchiveNotFoundError(url)
        return payload.getvalue()

    progress: list[DownloadProgress] = []
    with ParquetCandleArchive(tmp_path / "history") as archive:
        results = download_history(
            archive,
            fetch=fetch,
            symbols=["BTWUSDT"],
            timeframes=["1s"],
            start=datetime(2026, 5, 31, tzinfo=UTC),
            end=datetime(2026, 6, 2, tzinfo=UTC),
            on_progress=progress.append,
        )

    assert [(item.period, item.unavailable, item.rows) for item in results] == [
        ("2026-05-31", True, 0),
        ("2026-06-01", False, 1),
    ]
    assert [item.phase for item in progress if item.phase == "unavailable"] == [
        "unavailable"
    ]
    assert progress
    assert all(item.worker_id > 0 for item in progress)


def test_download_history_rechecks_disk_space_before_each_download_and_write(
    tmp_path,
):
    payload = BytesIO()
    with zipfile.ZipFile(payload, "w") as source:
        source.writestr(
            "AKEUSDT-aggTrades-2026-07-01.csv",
            "agg_trade_id,price,quantity,first_trade_id,last_trade_id,"
            "transact_time,is_buyer_maker\n"
            "1,1.0,2.0,1,1,1782864000100,false\n",
        )
    fetch = MagicMock(return_value=payload.getvalue())
    checks = 0
    progress: list[DownloadProgress] = []

    def storage_check() -> None:
        nonlocal checks
        checks += 1
        if checks == 3:
            raise RuntimeError("insufficient disk space")

    with ParquetCandleArchive(tmp_path / "history") as archive:
        with pytest.raises(RuntimeError, match="insufficient disk space"):
            download_history(
                archive,
                fetch=fetch,
                symbols=["AKEUSDT"],
                timeframes=["1s"],
                start=datetime(2026, 7, 1, tzinfo=UTC),
                end=datetime(2026, 7, 3, tzinfo=UTC),
                storage_check=storage_check,
                on_progress=progress.append,
            )

    assert fetch.call_count == 1
    assert checks == 3
    assert progress[-1].phase == "failed"
    assert progress[-1].worker_id > 0
    assert "insufficient disk space" in progress[-1].error
    assert (tmp_path / "history/AKEUSDT/1s/2026/07/01/candles.parquet").is_file()


def test_http_fetcher_reports_retries_before_succeeding():
    content = b"eventual archive"
    checksum = hashlib.sha256(content).hexdigest()
    requests = 0
    retries: list[tuple[int, int]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        if request.url.path.endswith(".CHECKSUM"):
            return httpx.Response(200, text=checksum)
        requests += 1
        if requests < 3:
            raise httpx.ConnectError("temporary outage", request=request)
        return httpx.Response(200, content=content)

    def on_retry(
        url: str, attempt: int, attempts: int, error: Exception
    ) -> None:
        retries.append((attempt, attempts))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = BinanceVisionHTTPFetcher(
            client,
            attempts=3,
            retry_base_seconds=0,
            on_retry=on_retry,
        )("https://example.com/archive.zip")

    assert result == content
    assert requests == 3
    assert retries == [(2, 3), (3, 3)]


def test_parquet_archive_allows_separate_archive_handles(tmp_path):
    root = tmp_path / "history"
    with ParquetCandleArchive(root), ParquetCandleArchive(root):
        pass
