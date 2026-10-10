import json
import os
import uuid
from decimal import Decimal

import httpx
import pytest
import redis.asyncio as redis
from redis.exceptions import ResponseError

from trading_platform.market.main import MarketLayerConfig, MarketLayerService, create_app
from trading_platform.shared.events import Kline
from trading_platform.market.store.kline_store import KlineStore


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


async def test_completed_kline_commit_is_idempotent_and_replayable(real_redis):
    symbol = "T" + uuid.uuid4().hex[:12].upper()
    store = KlineStore(real_redis)
    stream = f"kline:stream:{symbol}:1m"
    keys = [stream, f"kline:{symbol}:1m", f"market:published:{stream}"]
    try:
        for timestamp in [0, 60_000, 60_000, 120_000]:
            await store.store_kline(Kline(symbol=symbol, interval="1m", open_time=timestamp,
                close_time=timestamp + 59_999, available_time=timestamp + 60_000,
                open=Decimal("1"), high=Decimal("1"), low=Decimal("1"),
                close=Decimal("1"), volume=Decimal("1")))
        rows = await real_redis.xrange(stream)
        assert [json.loads(fields[b"data"])["open_time"] for _, fields in rows] == [0, 60_000, 120_000]
        assert (await store.get_latest_kline(symbol, "1m"))["open_time"] == 120_000
        with pytest.raises(ResponseError, match="conflicting"):
            await store.store_kline(Kline(symbol=symbol, interval="1m", open_time=120_000,
                close_time=179_999, available_time=180_000,
                open=Decimal("1"), high=Decimal("2"), low=Decimal("1"),
                close=Decimal("2"), volume=Decimal("1")))
        assert await real_redis.xlen(stream) == 3
    finally:
        await real_redis.delete(*keys)
        await real_redis.hdel("market:continuity_watermarks:v2", f"{symbol.lower()}@kline_1m")


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

        older = Kline(
            symbol=symbol,
            interval="1m",
            open_time=0,
            close_time=59_999,
            available_time=60_000,
            open=Decimal("1"),
            high=Decimal("2"),
            low=Decimal("1"),
            close=Decimal("1"),
            volume=Decimal("1"),
        )
        await service._handle_kline(symbol, "1m", older)
        latest_after_replay = await service.kline_store.get_latest_kline(symbol, "1m")
        assert latest_after_replay["close_time"] == 60999
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
