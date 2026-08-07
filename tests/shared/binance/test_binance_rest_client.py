from unittest.mock import AsyncMock

import pytest

from trading_platform.shared.binance import BinanceRestClient


def _client() -> BinanceRestClient:
    return BinanceRestClient(api_key="test-key", api_secret="test-secret")


@pytest.mark.asyncio
async def test_get_agg_trades_builds_public_request_and_returns_list():
    client = _client()
    payload = [{"a": 1, "p": "100.0", "q": "0.5", "T": 1234567890}]
    client._request = AsyncMock(return_value=payload)

    try:
        result = await client.get_agg_trades(
            "BTCUSDT", start_time=1000, end_time=2000, limit=100
        )
    finally:
        await client.close()

    assert result == payload
    client._request.assert_awaited_once_with(
        "GET",
        "/fapi/v1/aggTrades",
        {
            "symbol": "BTCUSDT",
            "limit": 100,
            "startTime": 1000,
            "endTime": 2000,
        },
        signed=False,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs", [{"start_time": 1000}, {"end_time": 2000}])
async def test_get_agg_trades_rejects_from_id_with_time_range(kwargs):
    client = _client()
    client._request = AsyncMock()
    try:
        with pytest.raises(ValueError, match="from_id"):
            await client.get_agg_trades("BTCUSDT", from_id=123, **kwargs)
    finally:
        await client.close()

    client._request.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, -1, 1001])
async def test_get_agg_trades_rejects_limit_out_of_range(limit):
    client = _client()
    client._request = AsyncMock()
    try:
        with pytest.raises(ValueError, match="limit must be between 1 and 1000"):
            await client.get_agg_trades("BTCUSDT", limit=limit)
    finally:
        await client.close()

    client._request.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_agg_trades_rejects_non_list_response():
    client = _client()
    client._request = AsyncMock(return_value={"error": "unexpected"})
    try:
        with pytest.raises(RuntimeError, match="invalid Binance aggregate trades response"):
            await client.get_agg_trades("BTCUSDT")
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_get_klines_supports_deterministic_time_range():
    client = _client()
    client._request = AsyncMock(return_value=[])
    try:
        await client.get_klines(
            "BTCUSDT", "1m", limit=1500, start_time=60_000, end_time=180_000
        )
    finally:
        await client.close()
    client._request.assert_awaited_once_with(
        "GET", "/fapi/v1/klines",
        {"symbol": "BTCUSDT", "interval": "1m", "limit": 1500,
         "startTime": 60_000, "endTime": 180_000},
        signed=False,
    )
