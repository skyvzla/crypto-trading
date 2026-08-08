from __future__ import annotations

import duckdb
import httpx

from trading_platform.market.archive.verify import (
    BinanceUSDMKlineFetcher,
    OfficialKline,
    main,
    verify_history,
)


def _create_history(path, *, one_minute_close: float = 159.0) -> None:
    connection = duckdb.connect(str(path))
    connection.execute("SET TimeZone = 'Asia/Shanghai'")
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
    start_ms = 1_782_864_000_000
    rows = [
        (
            "AKEUSDT",
            "1s",
            start_ms + second * 1_000,
            100.0 + second,
            101.0 + second,
            99.0 + second,
            100.0 + second,
            1.0,
            start_ms + (second + 1) * 1_000 - 1,
        )
        for second in range(60)
    ]
    rows.append(
        (
            "AKEUSDT",
            "1m",
            start_ms,
            100.0,
            160.0,
            99.0,
            one_minute_close,
            60.0,
            start_ms + 60_000 - 1,
        )
    )
    connection.executemany(
        """
        INSERT INTO candles VALUES (
            ?, ?, to_timestamp(? / 1000.0), ?, ?, ?, ?, ?,
            to_timestamp(? / 1000.0)
        )
        """,
        rows,
    )
    connection.close()


def test_verify_history_aggregates_1s_by_epoch_minute_in_utc(tmp_path):
    path = tmp_path / "history.duckdb"
    _create_history(path)

    report = verify_history(path)

    assert report.passed is True
    assert len(report.symbols) == 1
    symbol = report.symbols[0]
    assert symbol.symbol == "AKEUSDT"
    assert symbol.matched_minutes == 1
    assert symbol.archive_max_relative_error == 0.0
    assert symbol.official_samples == 0
    assert symbol.official_max_relative_error is None
    assert symbol.sampled_open_times == ()


def test_verify_history_samples_binance_with_epoch_ms_and_applies_tolerance(tmp_path):
    path = tmp_path / "history.duckdb"
    _create_history(path)
    calls: list[tuple[str, int]] = []

    def fetch(symbol: str, open_time_ms: int) -> OfficialKline:
        calls.append((symbol, open_time_ms))
        return OfficialKline(
            open_time_ms=open_time_ms,
            open=100.0,
            high=160.0,
            low=99.0,
            close=160.0,
        )

    report = verify_history(
        path,
        tolerance=0.001,
        official_fetcher=fetch,
        official_sample_count=5,
    )

    assert calls == [("AKEUSDT", 1_782_864_000_000)]
    assert report.symbols[0].official_samples == 1
    assert report.symbols[0].official_max_relative_error == 1.0 / 160.0
    assert report.passed is False


def test_binance_usdm_fetcher_reads_official_one_minute_kline():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/fapi/v1/klines"
        assert dict(request.url.params) == {
            "symbol": "AKEUSDT",
            "interval": "1m",
            "startTime": "1782864000000",
            "endTime": "1782864059999",
            "limit": "1",
        }
        return httpx.Response(
            200,
            json=[
                [
                    1_782_864_000_000,
                    "100.0",
                    "160.0",
                    "99.0",
                    "159.0",
                    "60",
                    1_782_864_059_999,
                ]
            ],
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = BinanceUSDMKlineFetcher(client)("akeusdt", 1_782_864_000_000)

    assert result == OfficialKline(
        open_time_ms=1_782_864_000_000,
        open=100.0,
        high=160.0,
        low=99.0,
        close=159.0,
    )


def test_cli_returns_nonzero_and_prints_symbol_error_above_threshold(
    tmp_path, capsys
):
    path = tmp_path / "history.duckdb"
    _create_history(path, one_minute_close=160.0)

    exit_code = main(
        [str(path), "--tolerance", "0.001", "--official-samples", "0"]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "AKEUSDT" in captured.out
    assert "matched=1" in captured.out
    assert "archive_max_relative_error=0.00625" in captured.out


def test_cli_returns_nonzero_and_names_symbols_without_matching_minutes(
    tmp_path, capsys
):
    path = tmp_path / "history.duckdb"
    _create_history(path)
    connection = duckdb.connect(str(path))
    connection.execute("DELETE FROM candles WHERE timeframe = '1m'")
    connection.close()

    exit_code = main([str(path), "--official-samples", "0"])

    assert exit_code == 1
    assert "AKEUSDT: matched=0" in capsys.readouterr().out
