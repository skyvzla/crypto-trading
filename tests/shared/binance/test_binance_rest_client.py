from unittest.mock import AsyncMock

import httpx
import pytest

from trading_platform.shared.binance import (
    BinanceAPIException,
    BinanceRestClient,
    get_endpoint_weight,
)


def _client() -> BinanceRestClient:
    return BinanceRestClient(api_key="test-key", api_secret="test-secret")


def test_income_history_uses_binance_documented_request_weight():
    assert get_endpoint_weight("GET", "/fapi/v1/income") == 30


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


@pytest.mark.asyncio
async def test_get_income_history_builds_signed_request_and_returns_list():
    client = _client()
    payload = [
        {
            "symbol": "BTCUSDT",
            "incomeType": "FUNDING_FEE",
            "income": "-0.12",
            "asset": "USDT",
            "time": 1_700_000_000_000,
            "tranId": 123,
        }
    ]
    client._request = AsyncMock(return_value=payload)

    try:
        result = await client.get_income_history(
            symbol="BTCUSDT",
            income_type="FUNDING_FEE",
            start_time=1_699_999_000_000,
            end_time=1_700_001_000_000,
            limit=100,
        )
    finally:
        await client.close()

    assert result == payload
    client._request.assert_awaited_once_with(
        "GET",
        "/fapi/v1/income",
        {
            "symbol": "BTCUSDT",
            "incomeType": "FUNDING_FEE",
            "startTime": 1_699_999_000_000,
            "endTime": 1_700_001_000_000,
            "limit": 100,
        },
    )


@pytest.mark.asyncio
async def test_get_income_history_supports_page_number():
    client = _client()
    client._request = AsyncMock(return_value=[])
    try:
        await client.get_income_history(page=2, limit=25)
    finally:
        await client.close()

    client._request.assert_awaited_once_with(
        "GET", "/fapi/v1/income", {"limit": 25, "page": 2}
    )


@pytest.mark.asyncio
async def test_get_income_history_rejects_non_positive_page():
    client = _client()
    client._request = AsyncMock()
    try:
        with pytest.raises(ValueError, match="page must be positive"):
            await client.get_income_history(page=0)
    finally:
        await client.close()

    client._request.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, 1001])
async def test_get_income_history_rejects_limit_out_of_range(limit):
    client = _client()
    client._request = AsyncMock()
    try:
        with pytest.raises(ValueError, match="limit must be between 1 and 1000"):
            await client.get_income_history(limit=limit)
    finally:
        await client.close()

    client._request.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"start_time": -1}, "start_time must be non-negative"),
        ({"end_time": -1}, "end_time must be non-negative"),
        (
            {"start_time": 2_000, "end_time": 1_000},
            "start_time must not be after end_time",
        ),
    ],
)
async def test_get_income_history_rejects_invalid_time_range(kwargs, message):
    client = _client()
    client._request = AsyncMock()
    try:
        with pytest.raises(ValueError, match=message):
            await client.get_income_history(**kwargs)
    finally:
        await client.close()

    client._request.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"symbol": "  "}, "symbol must not be blank"),
        ({"income_type": ""}, "income_type must not be blank"),
    ],
)
async def test_get_income_history_rejects_blank_optional_filters(kwargs, message):
    client = _client()
    client._request = AsyncMock()
    try:
        with pytest.raises(ValueError, match=message):
            await client.get_income_history(**kwargs)
    finally:
        await client.close()

    client._request.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_income_history_uses_signed_rest_request(monkeypatch):
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[])

    monkeypatch.setattr(
        "trading_platform.shared.binance.rest_client.time.time",
        lambda: 1_700_000_000.0,
    )
    client = _client()
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        base_url=client.base_url,
        transport=httpx.MockTransport(handler),
        headers={"X-MBX-APIKEY": client.api_key},
    )
    try:
        assert await client.get_income_history(limit=1) == []
    finally:
        await client.close()

    assert len(seen) == 1
    request = seen[0]
    assert request.method == "GET"
    assert request.url.path == "/fapi/v1/income"
    assert request.url.params["limit"] == "1"
    assert request.url.params["timestamp"] == "1700000000000"
    assert request.url.params["signature"]
    assert request.headers["X-MBX-APIKEY"] == "test-key"


@pytest.mark.asyncio
async def test_get_income_history_preserves_binance_api_error_classification():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"code": -1121, "msg": "Invalid symbol."})

    client = _client()
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        base_url=client.base_url,
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(BinanceAPIException) as error:
            await client.get_income_history(symbol="INVALID", limit=1)
    finally:
        await client.close()

    assert error.value.code == -1121
    assert error.value.message == "Invalid symbol."


@pytest.mark.asyncio
async def test_get_income_history_rejects_non_list_response():
    client = _client()
    client._request = AsyncMock(return_value={"unexpected": "object"})
    try:
        with pytest.raises(RuntimeError, match="invalid Binance income history response"):
            await client.get_income_history()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_get_income_history_preserves_transport_timeout():
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out", request=request)

    client = _client()
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        base_url=client.base_url,
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(httpx.ReadTimeout, match="read timed out"):
            await client.get_income_history(limit=1)
    finally:
        await client.close()
