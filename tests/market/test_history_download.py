import hashlib
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from io import BytesIO
from threading import Barrier
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
        data_dir="unused",
        symbols=["AKEUSDT"],
        start_ms=1_782_864_000_000,
        end_ms=1_782_864_060_000,
        require_aggtrades=True,
        required_kline_intervals=["1m"],
        duckdb_path=str(catalog),
    ).load_all()
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


def test_worker_pool_keeps_a_fetcher_bound_to_a_worker_thread():
    calls: list[str] = []

    class Fetcher:
        def __init__(self, name: str) -> None:
            self.name = name

        def __call__(self, url: str) -> bytes:
            calls.append(self.name)
            return self.name.encode()

    pool = BinanceVisionWorkerPoolFetcher([Fetcher("a"), Fetcher("b")])

    assert [pool("first"), pool("second"), pool("third")] == [
        b"a",
        b"a",
        b"a",
    ]
    assert calls == ["a", "a", "a"]


def test_worker_pool_assigns_a_different_fetcher_to_each_worker_thread():
    barrier = Barrier(2)

    class Fetcher:
        def __init__(self, name: str) -> None:
            self.name = name

        def __call__(self, _url: str) -> bytes:
            return self.name.encode()

    pool = BinanceVisionWorkerPoolFetcher([Fetcher("a"), Fetcher("b")])
    def fetch_twice() -> tuple[bytes, bytes]:
        first = pool("first")
        barrier.wait(timeout=1)
        return first, pool("second")

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _value: fetch_twice(), range(2)))

    assert {result[0] for result in results} == {b"a", b"b"}
    assert all(first == second for first, second in results)


def test_cli_progress_uses_monotonic_completion_count(capsys):
    reporter = _ProgressReporter(workers=4)
    reporter.retry(
        "https://example.com/BTWUSDT-aggTrades-2026-06-04.zip",
        2,
        3,
        httpx.ConnectError("temporary outage"),
        proxy="socks5h://proxy-a:1080",
    )
    reporter(
        DownloadProgress(
            phase="downloaded",
            downloaded_bytes=2 * 1024 * 1024,
            elapsed_seconds=0.5,
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
            current=68,
            total=68,
            symbol="BANKUSDT",
            timeframe="15m",
            period="2026-07",
            rows=2976,
        )
    )
    reporter(
        DownloadProgress(
            phase="skipped",
            current=64,
            total=68,
            symbol="BANKUSDT",
            timeframe="1s",
            period="2026-07-30",
            rows=86249,
        )
    )

    output = capsys.readouterr().err
    assert (
        "Retry 2/3 BTWUSDT-aggTrades-2026-06-04.zip "
        "proxy=socks5h://proxy-a:1080" in output
    )
    assert "Processing 68 files with 4 workers." in output
    assert "[1/68] BANKUSDT 15m 2026-07 stored 2976 rows" in output
    assert "2.0 MiB at 4.0 MiB/s" in output
    assert "[2/68] BANKUSDT 1s 2026-07-30 skipped" in output
    assert "[68/68]" not in output


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
        "create_duckdb_catalog",
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


def test_cli_uses_thread_bound_proxy_pool(
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
        "create_duckdb_catalog",
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
    assert "Using 2 thread-bound proxies with 2 workers." in capsys.readouterr().err


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
        "create_duckdb_catalog",
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
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 130
    assert captured.out == ""
    assert captured.err == (
        "Downloading data for 1 trading pair.\n"
        "Cancelled.\n"
    )
    assert "Traceback" not in captured.err


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
            )

    assert fetch.call_count == 1
    assert checks == 3
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
