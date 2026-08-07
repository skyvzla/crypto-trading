import os
import uuid
from unittest.mock import AsyncMock, Mock

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


async def test_old_campaign_json_defaults_candidate_exit_state():
    redis_client = Mock(
        get=AsyncMock(
            return_value=(
                b'{"campaign_id":"campaign-1","strategy_id":"spike_short",'
                b'"symbol":"BTCUSDT","started_at_ms":1000}'
            )
        )
    )
    store = RedisCampaignStore(redis_client)

    assert await store.get_active() == lease()


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


async def test_exit_state_update_requires_same_campaign_and_preserves_lease(real_redis):
    store = RedisCampaignStore(real_redis, key=f"test:campaign:{uuid.uuid4().hex}")
    original = CampaignLease(
        "campaign-1",
        "spike_short",
        "BTCUSDT",
        1_000,
        origin_price="0.4321",
    )
    try:
        assert await store.acquire(original) is True
        assert await store.update_exit_state(
            "campaign-2",
            origin_checked=True,
            reduced_at_origin=True,
            exit_requested=True,
        ) is False
        assert await store.get_active() == original

        assert await store.update_exit_state(
            "campaign-1",
            origin_checked=True,
            reduced_at_origin=True,
            exit_requested=False,
        ) is True
        assert await store.get_active() == CampaignLease(
            "campaign-1",
            "spike_short",
            "BTCUSDT",
            1_000,
            origin_price="0.4321",
            origin_checked=True,
            reduced_at_origin=True,
            exit_requested=False,
        )
    finally:
        await store.release("campaign-1")


async def test_reduced_at_origin_survives_store_restart(real_redis):
    key = f"test:campaign:{uuid.uuid4().hex}"
    first_process = RedisCampaignStore(real_redis, key=key)
    try:
        assert await first_process.acquire(lease()) is True
        assert await first_process.update_exit_state(
            "campaign-1",
            origin_checked=True,
            reduced_at_origin=True,
            exit_requested=False,
        ) is True

        restarted_process = RedisCampaignStore(real_redis, key=key)
        recovered = await restarted_process.get_active()
        assert recovered is not None
        assert recovered.origin_checked is True
        assert recovered.reduced_at_origin is True
        assert recovered.exit_requested is False
    finally:
        await first_process.release("campaign-1")


async def test_exit_state_update_fails_after_release(real_redis):
    store = RedisCampaignStore(real_redis, key=f"test:campaign:{uuid.uuid4().hex}")
    assert await store.acquire(lease()) is True
    assert await store.release("campaign-1") is True

    assert await store.update_exit_state(
        "campaign-1",
        origin_checked=True,
        reduced_at_origin=True,
        exit_requested=True,
    ) is False
    assert await store.get_active() is None
