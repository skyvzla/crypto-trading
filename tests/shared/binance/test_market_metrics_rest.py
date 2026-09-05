from unittest.mock import AsyncMock

import pytest

from trading_platform.shared.binance.rest_client import BinanceRestClient


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "path"),
    [
        ("get_open_interest_history", "/futures/data/openInterestHist"),
        (
            "get_global_long_short_account_ratio",
            "/futures/data/globalLongShortAccountRatio",
        ),
    ],
)
async def test_public_metrics_methods_build_unsigned_5m_requests(method_name, path):
    client = BinanceRestClient(api_key="", api_secret="")
    client._request = AsyncMock(return_value=[])
    try:
        result = await getattr(client, method_name)("BTCUSDT", period="5m", limit=2)
    finally:
        await client.close()

    assert result == []
    client._request.assert_awaited_once_with(
        "GET",
        path,
        {"symbol": "BTCUSDT", "period": "5m", "limit": 2},
        signed=False,
    )


@pytest.mark.asyncio
async def test_public_metrics_methods_reject_unsupported_period_before_request():
    client = BinanceRestClient(api_key="", api_secret="")
    client._request = AsyncMock()
    try:
        with pytest.raises(ValueError, match="only the 5m"):
            await client.get_open_interest_history("BTCUSDT", period="15m")
    finally:
        await client.close()
    client._request.assert_not_awaited()


@pytest.mark.asyncio
async def test_public_metrics_methods_reject_non_list_response():
    client = BinanceRestClient(api_key="", api_secret="")
    client._request = AsyncMock(return_value={"code": -1})
    try:
        with pytest.raises(RuntimeError, match="long/short ratio"):
            await client.get_global_long_short_account_ratio("BTCUSDT")
    finally:
        await client.close()
