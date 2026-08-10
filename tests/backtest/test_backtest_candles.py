from datetime import UTC, datetime, timedelta

import httpx
import pytest

from trading_platform.backtest.candles import (
    fetch_binance_candles,
    load_archive_candles,
    validate_candle_request,
)
from trading_platform.market.archive.index import build_archive_index
from trading_platform.market.archive.models import Candle
from trading_platform.market.archive.parquet import ParquetCandleArchive


def test_candle_request_rejects_unbounded_or_unsupported_queries():
    with pytest.raises(ValueError, match="unsupported"):
        validate_candle_request("BTCUSDT", "2m", 0, 60_000)
    with pytest.raises(ValueError, match="5000"):
        validate_candle_request("BTCUSDT", "1m", 0, 5001 * 60_000)
    with pytest.raises(ValueError, match="symbol"):
        validate_candle_request("BTC/USDT", "1m", 0, 60_000)


@pytest.mark.asyncio
async def test_binance_reader_maps_public_kline_payload():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/fapi/v1/klines"
        assert request.url.params["symbol"] == "AKEUSDT"
        return httpx.Response(
            200,
            json=[
                [60_000, "1", "2", "0.5", "1.5", "42", 119_999],
                [120_000, "1.5", "3", "1", "2", "50", 179_999],
            ],
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://fapi.binance.com"
    ) as client:
        rows = await fetch_binance_candles(
            "akeusdt", "1m", 60_000, 180_000, client=client
        )

    assert rows == [
        {"time": 60, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 42.0},
        {"time": 120, "open": 1.5, "high": 3.0, "low": 1.0, "close": 2.0, "volume": 50.0},
    ]


def test_archive_reader_aggregates_one_minute_candles(tmp_path):
    start = datetime(2025, 7, 1, tzinfo=UTC)
    rows = [
        Candle(
            symbol="AKEUSDT",
            timeframe="1m",
            open_time=start + timedelta(minutes=index),
            open=1 + index,
            high=2 + index,
            low=0.5 + index,
            close=1.5 + index,
            volume=10 + index,
            close_time=start + timedelta(minutes=index + 1) - timedelta(milliseconds=1),
        )
        for index in range(5)
    ]
    with ParquetCandleArchive(tmp_path, rebuild_index_on_close=False) as archive:
        archive.upsert(rows)
    index = build_archive_index(tmp_path, workers=1)

    candles = load_archive_candles(
        index,
        "AKEUSDT",
        "5m",
        int(start.timestamp() * 1000),
        int((start + timedelta(minutes=5)).timestamp() * 1000),
    )

    assert candles == [
        {
            "time": int(start.timestamp()),
            "open": 1.0,
            "high": 6.0,
            "low": 0.5,
            "close": 5.5,
            "volume": 60.0,
        }
    ]
