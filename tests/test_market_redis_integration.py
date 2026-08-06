import json
import os
import uuid
from decimal import Decimal

import httpx
import pytest
import redis.asyncio as redis

from trading_platform.market.main import MarketLayerConfig, MarketLayerService, create_app
from trading_platform.shared.events import Kline


@pytest.fixture
async def real_redis():
    url = os.getenv("TEST_REDIS_URL")
    if not url:
        pytest.skip("TEST_REDIS_URL is required for Redis integration tests")

    client = redis.from_url(url, decode_responses=False)
    try:
        await client.ping()
    except Exception as exc:
        await client.aclose()
        pytest.fail(f"TEST_REDIS_URL is unavailable: {exc}")

    try:
        yield client
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_market_service_publishes_bar_and_round_trips_kline(real_redis):
    suffix = uuid.uuid4().hex[:12].upper()
    symbol = f"T{suffix}"
    channel = f"bar1s:{symbol}"
    kline_key = f"kline:{symbol}:1m"
    service = MarketLayerService(MarketLayerConfig(), real_redis, "integration-epoch")
    pubsub = real_redis.pubsub()

    try:
        await pubsub.subscribe(channel)
        subscription = await pubsub.get_message(timeout=2)
        assert subscription is not None
        assert subscription["type"] == "subscribe"

        await service._handle_aggtrade(
            symbol,
            {"price": Decimal("100"), "quantity": Decimal("2"), "timestamp": 1000},
        )
        await service._handle_aggtrade(
            symbol,
            {"price": Decimal("102"), "quantity": Decimal("3"), "timestamp": 2000},
        )

        published = await pubsub.get_message(ignore_subscribe_messages=True, timeout=2)
        assert published is not None
        payload = json.loads(published["data"])
        assert payload == {
            "symbol": symbol,
            "timestamp": 1000,
            "available_time": 2000,
            "open": "100",
            "high": "100",
            "low": "100",
            "close": "100",
            "volume": "2",
            "trade_count": 1,
            "vwap": "100",
        }

        kline = Kline(
            symbol=symbol,
            interval="1m",
            open_time=1000,
            close_time=60999,
            available_time=61000,
            open=Decimal("100"),
            high=Decimal("105"),
            low=Decimal("99"),
            close=Decimal("103"),
            volume=Decimal("42"),
        )
        await service._handle_kline(symbol, "1m", kline)

        latest = await service.kline_store.get_latest_kline(symbol, "1m")
        assert latest is not None
        assert latest["symbol"] == symbol
        assert latest["close_time"] == 60999
        assert latest["close"] == "103"
    finally:
        await pubsub.aclose()
        await real_redis.delete(kline_key)


@pytest.mark.asyncio
async def test_health_uses_real_redis_dependency(real_redis):
    app, service = create_app(MarketLayerConfig(), "integration-epoch")
    await service.redis.aclose()
    service.redis = real_redis

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json()["redis_connected"] is True
