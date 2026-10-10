from decimal import Decimal

import httpx
import pytest

from trading_platform.market.history import closed_kline_window, fetch_closed_klines, validate_kline_history
from trading_platform.shared.events import Kline
from trading_platform.strategies.spike.v2_2 import V22


def candle(timestamp):
    return Kline(symbol="BTCUSDT", interval="1m", open_time=timestamp,
                 close_time=timestamp + 59_999, available_time=timestamp + 60_000,
                 open=Decimal("1"), high=Decimal("1"), low=Decimal("1"),
                 close=Decimal("1"), volume=Decimal("1"))


def test_default_v22_requires_seven_days_independent_of_retention():
    assert V22.data_requirements.resolve_required_history(V22.defaults) == {
        "1m": 10080, "5m": 15, "15m": 10,
    }


def test_history_rejects_a_missing_minute_and_conflicting_duplicate():
    arguments = dict(symbol="BTCUSDT", interval="1m", start_ms=0, end_ms=180_000, as_of_ms=180_000)
    with pytest.raises(ValueError, match="missing.*60000"):
        validate_kline_history([candle(0), candle(120_000)], **arguments)
    altered = candle(60_000)
    altered.close = Decimal("2")
    with pytest.raises(ValueError, match="conflicting"):
        validate_kline_history([candle(0), candle(60_000), altered], **arguments)


async def test_seven_day_warmup_pages_to_one_fixed_closed_cutoff():
    end = 10_080 * 60_000
    pages = []
    def respond(request):
        start = int(request.url.params["start_time"])
        stop = int(request.url.params["end_time"]) + 1
        pages.append((start, stop))
        return httpx.Response(200, json={"klines": [candle(t).to_dict() for t in range(start, stop, 60_000)]})
    async with httpx.AsyncClient(base_url="http://market", transport=httpx.MockTransport(respond)) as client:
        result = await fetch_closed_klines(client, "BTCUSDT", "1m", start_ms=0, end_ms=end)
    assert len(result) == 10_080
    assert len(pages) == 7
    assert pages[0][0] == 0 and pages[-1][1] == end
    assert all(left[1] == right[0] for left, right in zip(pages, pages[1:]))
    assert closed_kline_window("1m", 10_080, end + 10_000) == (0, end)


async def test_short_page_cannot_be_reported_as_complete_warmup():
    async with httpx.AsyncClient(base_url="http://market", transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"klines": [candle(0).to_dict()]})
    )) as client:
        with pytest.raises(ValueError, match="missing"):
            await fetch_closed_klines(client, "BTCUSDT", "1m", start_ms=0, end_ms=120_000)
