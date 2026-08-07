import os
import uuid

import pytest
import redis.asyncio as redis

from trading_platform.strategies.campaign_store import CampaignLease, RedisCampaignStore


@pytest.fixture
async def real_redis():
    url = os.getenv("TEST_REDIS_URL")
    if not url:
        pytest.skip("TEST_REDIS_URL is required for Redis integration tests")
    client = redis.from_url(url, decode_responses=False)
    try:
        await client.ping()
        yield client
    finally:
        await client.aclose()


def lease(campaign_id="campaign-1"):
    return CampaignLease(campaign_id, "spike_short", "BTCUSDT", 1_000)


async def test_campaign_store_allows_only_one_global_campaign(real_redis):
    store = RedisCampaignStore(real_redis, key=f"test:campaign:{uuid.uuid4().hex}")
    try:
        assert await store.acquire(lease()) is True
        assert await store.acquire(lease("campaign-2")) is False
        assert await store.get_active() == lease()
    finally:
        await store.release("campaign-1")


async def test_campaign_release_requires_owner(real_redis):
    store = RedisCampaignStore(real_redis, key=f"test:campaign:{uuid.uuid4().hex}")
    await store.acquire(lease())

    assert await store.release("campaign-2") is False
    assert await store.get_active() == lease()
    assert await store.release("campaign-1") is True
    assert await store.get_active() is None
