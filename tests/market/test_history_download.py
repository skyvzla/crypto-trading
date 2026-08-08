import hashlib
import zipfile
from datetime import UTC, datetime
from io import BytesIO

import duckdb
import httpx
import pytest

from trading_platform.backtest.loader import BacktestDataLoader
from trading_platform.market.archive import vision as archive_vision
from trading_platform.market.archive import (
    BinanceVisionHTTPFetcher,
    Candle,
    DownloadProgress,
    DownloadResult,
    ParquetCandleArchive,
    aggtrade_archive_url,
    create_duckdb_catalog,
    download_history,
    kline_archive_url,
    parse_aggtrade_archive,
    parse_kline_archive,
)
from trading_platform.market.archive import cli as archive_cli
from trading_platform.market.archive.cli import _ProgressReporter, _print_result


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


def test_cli_progress_uses_monotonic_completion_count(capsys):
    reporter = _ProgressReporter(workers=4)
    reporter.retry(
        "https://example.com/BTWUSDT-aggTrades-2026-06-04.zip",
        2,
        3,
        httpx.ConnectError("temporary outage"),
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
    assert "Retry 2/3 BTWUSDT-aggTrades-2026-06-04.zip" in output
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
    assert captured.err == "Cancelled.\n"
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
