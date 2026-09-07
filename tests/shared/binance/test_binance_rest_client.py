import asyncio
from decimal import Decimal
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
async def test_test_order_uses_non_matching_signed_endpoint():
    client = _client()
    client._request = AsyncMock(return_value={})

    try:
        result = await client.test_order(
            symbol="BTCUSDT",
            side="SELL",
            order_type="LIMIT",
            quantity=Decimal("0.001"),
            price=Decimal("100000"),
            new_client_order_id="tp_preflight_1",
        )
    finally:
        await client.close()

    assert result == {}
    client._request.assert_awaited_once_with(
        "POST",
        "/fapi/v1/order/test",
        {
            "symbol": "BTCUSDT",
            "side": "SELL",
            "type": "LIMIT",
            "quantity": "0.001",
            "price": "100000",
            "timeInForce": "GTC",
            "newClientOrderId": "tp_preflight_1",
        },
    )


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


@pytest.mark.asyncio
async def test_request_observer_reports_order_and_redacts_sensitive_fields():
    observed: list[tuple[str, dict[str, object]]] = []

    async def observer(stage: str, details: dict[str, object]) -> None:
        observed.append((stage, details))

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/fapi/v1/order"
        return httpx.Response(
            200,
            json={"ok": True, "token": "response-token"},
            headers={
                "X-MBX-USED-WEIGHT-1M": "11",
                "X-MBX-ORDER-COUNT-10S": "2",
                "X-Other": "ignored",
            },
        )

    client = BinanceRestClient(
        api_key="test-api-key",
        api_secret="test-api-secret",
        event_observer=observer,
    )
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        base_url=client.base_url,
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await client._request(
            "GET",
            "/fapi/v1/order?token=path-token",
            {
                "apiKey": "request-api-key",
                "listenKey": "request-listen-key",
                "password": "request-password",
                "symbol": "BTCUSDT",
            },
            signed=False,
        )
    finally:
        await client.close()

    assert result == {"ok": True, "token": "response-token"}
    assert [stage for stage, _ in observed] == [
        "request_started",
        "request_succeeded",
    ]
    started = observed[0][1]
    succeeded = observed[1][1]
    assert started["method"] == "GET"
    assert started["status_code"] is None
    assert started["params"]["apiKey"] == "[REDACTED]"
    assert started["params"]["listenKey"] == "[REDACTED]"
    assert started["params"]["password"] == "[REDACTED]"
    assert started["path"] == "/fapi/v1/order?token=[REDACTED]"
    assert succeeded["status_code"] == 200
    assert succeeded["rate_headers"] == {
        "X-MBX-USED-WEIGHT-1M": "11",
        "X-MBX-ORDER-COUNT-10S": "2",
    }
    assert succeeded["response_body"] == {
        "ok": True,
        "token": "[REDACTED]",
    }
    assert isinstance(succeeded["duration_ms"], (int, float))


@pytest.mark.asyncio
async def test_request_observer_reports_business_error_once_with_body():
    observed: list[tuple[str, dict[str, object]]] = []

    def observer(stage: str, details: dict[str, object]) -> None:
        observed.append((stage, details))

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"code": -1121, "msg": "Invalid symbol", "token": "secret"},
        )

    client = BinanceRestClient(
        api_key="api-key",
        api_secret="api-secret",
        event_observer=observer,
    )
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        base_url=client.base_url,
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(BinanceAPIException) as error:
            await client._request("GET", "/fapi/v1/order", {}, signed=False)
    finally:
        await client.close()

    assert error.value.code == -1121
    assert [stage for stage, _ in observed] == [
        "request_started",
        "request_failed",
    ]
    failed = observed[1][1]
    assert failed["status_code"] == 400
    assert failed["response_body"]["token"] == "[REDACTED]"
    assert failed["error"] == "Binance API Error -1121: Invalid symbol"


@pytest.mark.asyncio
async def test_request_observer_reports_timeout_and_propagates_timeout():
    observed: list[str] = []

    async def observer(stage: str, _details: dict[str, object]) -> None:
        observed.append(stage)

    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out", request=request)

    client = BinanceRestClient(
        api_key="api-key", api_secret="api-secret", event_observer=observer
    )
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        base_url=client.base_url,
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(httpx.ReadTimeout, match="read timed out"):
            await client._request("GET", "/fapi/v1/order", {}, signed=False)
    finally:
        await client.close()

    assert observed == ["request_started", "request_failed"]


@pytest.mark.asyncio
async def test_request_observer_failure_before_http_prevents_network():
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={})

    def observer(stage: str, _details: dict[str, object]) -> None:
        if stage == "request_started":
            raise RuntimeError("journal unavailable")

    client = BinanceRestClient(
        api_key="api-key", api_secret="api-secret", event_observer=observer
    )
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        base_url=client.base_url,
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(RuntimeError, match="journal unavailable"):
            await client._request("GET", "/fapi/v1/order", {}, signed=False)
    finally:
        await client.close()

    assert calls == 0


@pytest.mark.asyncio
async def test_request_observer_failure_after_http_is_propagated_without_duplicate_failure():
    observed: list[str] = []

    def observer(stage: str, _details: dict[str, object]) -> None:
        observed.append(stage)
        if stage == "request_succeeded":
            raise RuntimeError("journal write failed")

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"orderId": 42})

    client = BinanceRestClient(
        api_key="api-key", api_secret="api-secret", event_observer=observer
    )
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        base_url=client.base_url,
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(RuntimeError, match="journal write failed"):
            await client._request("POST", "/fapi/v1/order", {}, signed=False)
    finally:
        await client.close()

    assert observed == ["request_started", "request_succeeded"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "expected_code"),
    [
        (httpx.Response(400, json={"code": -1121, "msg": "Invalid symbol"}), -1121),
    ],
)
async def test_request_observer_failure_does_not_replace_binance_api_error(
    response: httpx.Response, expected_code: int
):
    async def observer(stage: str, _details: dict[str, object]) -> None:
        if stage == "request_failed":
            raise OSError("journal unavailable")

    async def handler(_request: httpx.Request) -> httpx.Response:
        return response

    client = BinanceRestClient(
        api_key="api-key", api_secret="api-secret", event_observer=observer
    )
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        base_url=client.base_url,
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(BinanceAPIException, match="Invalid symbol") as raised:
            await client._request("GET", "/fapi/v1/order", {}, signed=False)
    finally:
        await client.close()

    assert raised.value.code == expected_code
    assert any("request request_failed observer failed" in note for note in raised.value.__notes__)
    assert any("OSError: journal unavailable" in note for note in raised.value.__notes__)


@pytest.mark.asyncio
async def test_request_observer_failure_does_not_replace_timeout_error():
    async def observer(stage: str, _details: dict[str, object]) -> None:
        if stage == "request_failed":
            raise OSError("journal unavailable")

    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out", request=request)

    client = BinanceRestClient(
        api_key="api-key", api_secret="api-secret", event_observer=observer
    )
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        base_url=client.base_url,
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(httpx.ReadTimeout, match="read timed out") as raised:
            await client._request("GET", "/fapi/v1/order", {}, signed=False)
    finally:
        await client.close()

    assert any("request request_failed observer failed" in note for note in raised.value.__notes__)


@pytest.mark.asyncio
async def test_request_observer_failure_does_not_replace_response_parse_error():
    observed: list[tuple[str, dict[str, object]]] = []

    async def observer(stage: str, details: dict[str, object]) -> None:
        observed.append((stage, details))
        if stage == "request_failed":
            raise OSError("journal unavailable")

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not-json")

    client = BinanceRestClient(
        api_key="api-key", api_secret="api-secret", event_observer=observer
    )
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        base_url=client.base_url,
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(RuntimeError, match="Request failed") as raised:
            await client._request("GET", "/fapi/v1/order", {}, signed=False)
    finally:
        await client.close()

    assert isinstance(raised.value.__cause__, ValueError)
    assert any("request request_failed observer failed" in note for note in raised.value.__notes__)
    assert observed[-1][1]["error_type"] in {"JSONDecodeError", "ValueError"}


@pytest.mark.asyncio
async def test_request_cancelled_is_terminal_and_observer_failure_keeps_cancelled_error():
    observed: list[tuple[str, dict[str, object]]] = []

    async def observer(stage: str, details: dict[str, object]) -> None:
        observed.append((stage, details))
        if stage == "request_cancelled":
            raise OSError("journal unavailable")

    async def handler(_request: httpx.Request) -> httpx.Response:
        raise asyncio.CancelledError()

    client = BinanceRestClient(
        api_key="api-key", api_secret="api-secret", event_observer=observer
    )
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        base_url=client.base_url,
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(asyncio.CancelledError) as raised:
            await client._request("GET", "/fapi/v1/order", {}, signed=False)
    finally:
        await client.close()

    assert [stage for stage, _ in observed] == [
        "request_started",
        "request_cancelled",
    ]
    assert observed[-1][1]["error_type"] == "CancelledError"
    assert any("request request_cancelled observer failed" in note for note in raised.value.__notes__)
