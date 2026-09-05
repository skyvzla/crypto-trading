import asyncio
import json
import os
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import redis.asyncio as redis_async
from fastapi.testclient import TestClient

from trading_platform.market.api.routes import SubscriptionRequest
from trading_platform.market.main import MarketLayerService, create_app
from trading_platform.market.metrics import (
    METRICS_INTERVAL_MS,
    METRICS_STREAM_MAXLEN,
    MarketMetrics5m,
    MetricsRedisStore,
    MetricsValidationError,
    align_metrics_rows,
)
from trading_platform.shared.config import MarketLayerConfig


BUCKET = 1_800_000
NOW = BUCKET + METRICS_INTERVAL_MS


def _open_interest(timestamp=BUCKET, value="123.5"):
    return [{"symbol": "BTCUSDT", "timestamp": timestamp, "sumOpenInterest": value}]


def _long_short(timestamp=BUCKET, value="1.25"):
    return [{"symbol": "BTCUSDT", "timestamp": timestamp, "longShortRatio": value}]


def test_metrics_alignment_emits_only_the_fixed_completed_event_contract():
    event = align_metrics_rows(
        "btcusdt", _open_interest(), _long_short(), now_ms=NOW
    )

    assert event.to_dict() == {
        "symbol": "BTCUSDT",
        "available_time": NOW,
        "open_interest": 123.5,
        "long_short_ratio": 1.25,
    }


@pytest.mark.parametrize(
    ("open_interest", "long_short", "message"),
    [
        (_open_interest(BUCKET + 1), _long_short(), "aligned"),
        (_open_interest(), _long_short(BUCKET + METRICS_INTERVAL_MS), "common"),
        (_open_interest(value="nan"), _long_short(), "finite"),
    ],
)
def test_metrics_alignment_fails_closed_on_invalid_or_unmatched_rows(
    open_interest, long_short, message
):
    with pytest.raises(MetricsValidationError, match=message):
        align_metrics_rows("BTCUSDT", open_interest, long_short, now_ms=NOW)


def test_metrics_alignment_rejects_stale_common_bucket():
    with pytest.raises(MetricsValidationError, match="stale"):
        align_metrics_rows(
            "BTCUSDT",
            _open_interest(),
            _long_short(),
            now_ms=NOW + 2 * METRICS_INTERVAL_MS + 1,
        )


@pytest.mark.asyncio
async def test_metrics_store_atomically_writes_latest_and_replay_stream():
    redis = AsyncMock()
    redis.eval.return_value = 1
    event = MarketMetrics5m("BTCUSDT", NOW, 123.5, 1.25)

    assert await MetricsRedisStore(redis).publish(event) is True

    args = redis.eval.await_args.args
    assert args[1:5] == (
        2,
        "metrics:BTCUSDT:5m",
        "metrics:stream:BTCUSDT:5m",
        str(NOW),
    )
    message = args[5]
    assert json.loads(message) == event.to_dict()
    assert args[6] == str(METRICS_STREAM_MAXLEN)


def _integration_redis():
    url = os.getenv("TEST_REDIS_URL")
    if not url:
        pytest.skip("TEST_REDIS_URL is required for Redis CAS integration tests")
    return redis_async.from_url(url, decode_responses=True)


@pytest.mark.asyncio
async def test_metrics_store_concurrent_publish_never_rolls_back_watermark():
    client = _integration_redis()
    symbol = f"CAS{uuid4().hex.upper()}"
    key = f"metrics:{symbol}:5m"
    stream = f"metrics:stream:{symbol}:5m"
    store = MetricsRedisStore(client)
    older = MarketMetrics5m(symbol, NOW, 123.5, 1.25)
    newer = MarketMetrics5m(symbol, NOW + METRICS_INTERVAL_MS, 130.0, 1.5)
    try:
        results = await asyncio.gather(store.publish(older), store.publish(newer))

        assert results[1] is True
        assert await store.publish(older) is False
        assert await store.publish(newer) is False
        assert await client.hget(key, "watermark") == str(newer.available_time)
        assert json.loads(await client.hget(key, "latest")) == newer.to_dict()
        stream_events = [
            json.loads(fields["data"])
            for _, fields in await client.xrange(stream, min="-", max="+")
        ]
        assert stream_events[-1] == newer.to_dict()
        assert [item["available_time"] for item in stream_events] == sorted(
            item["available_time"] for item in stream_events
        )
    finally:
        await client.delete(key, stream)
        await client.aclose()


@pytest.mark.asyncio
async def test_metrics_store_rejects_concurrent_conflicting_duplicate():
    client = _integration_redis()
    symbol = f"CAS{uuid4().hex.upper()}"
    key = f"metrics:{symbol}:5m"
    stream = f"metrics:stream:{symbol}:5m"
    store = MetricsRedisStore(client)
    first = MarketMetrics5m(symbol, NOW, 123.5, 1.25)
    conflict = MarketMetrics5m(symbol, NOW, 999.0, 1.25)
    try:
        results = await asyncio.gather(
            store.publish(first), store.publish(conflict), return_exceptions=True
        )

        assert sum(result is True for result in results) == 1
        errors = [result for result in results if isinstance(result, Exception)]
        assert len(errors) == 1
        assert isinstance(errors[0], MetricsValidationError)
        assert "conflicting duplicate" in str(errors[0])
        assert len(await client.xrange(stream, min="-", max="+")) == 1
        assert await client.hget(key, "watermark") == str(NOW)
    finally:
        await client.delete(key, stream)
        await client.aclose()


@pytest.mark.asyncio
async def test_metrics_store_recovers_legacy_latest_watermark_atomically():
    client = _integration_redis()
    symbol = f"CAS{uuid4().hex.upper()}"
    key = f"metrics:{symbol}:5m"
    stream = f"metrics:stream:{symbol}:5m"
    store = MetricsRedisStore(client)
    event = MarketMetrics5m(symbol, NOW, 123.5, 1.25)
    try:
        await client.hset(key, "latest", json.dumps(event.to_dict()))

        assert await store.publish(event) is False
        assert await client.hget(key, "watermark") == str(NOW)
        assert await client.xlen(stream) == 0
    finally:
        await client.delete(key, stream)
        await client.aclose()


@pytest.mark.asyncio
async def test_market_collects_metrics_and_updates_quality():
    service = MarketLayerService(MarketLayerConfig(), AsyncMock(), "metrics-test")
    service.metrics_quality.set_expected_symbols({"BTCUSDT"})
    service.rest_client.get_open_interest_history = AsyncMock(
        return_value=_open_interest()
    )
    service.rest_client.get_global_long_short_account_ratio = AsyncMock(
        return_value=_long_short()
    )
    service.metrics_store.publish = AsyncMock(return_value=True)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr("trading_platform.market.main.time.time", lambda: NOW / 1000)
        await service._collect_metrics_symbol("BTCUSDT")

    event = service.metrics_store.publish.await_args.args[0]
    assert event.available_time == NOW
    assert service.metrics_quality.snapshot(now_ms=NOW)["BTCUSDT"]["status"] == "healthy"
    await service.rest_client.close()


@pytest.mark.asyncio
async def test_market_metrics_collection_failure_degrades_quality_without_publishing():
    service = MarketLayerService(MarketLayerConfig(), AsyncMock(), "metrics-test")
    service.metrics_quality.set_expected_symbols({"BTCUSDT"})
    service.rest_client.get_open_interest_history = AsyncMock(
        side_effect=RuntimeError("testnet endpoint returned non-JSON")
    )
    service.rest_client.get_global_long_short_account_ratio = AsyncMock(
        return_value=_long_short()
    )
    service.metrics_store.publish = AsyncMock()

    await service._collect_metrics_symbol("BTCUSDT")

    quality = service.metrics_quality.snapshot(now_ms=NOW)["BTCUSDT"]
    assert quality["status"] == "degraded"
    assert quality["issue"] == "metrics_collection_failed:RuntimeError"
    service.metrics_store.publish.assert_not_awaited()
    await service.rest_client.close()


@pytest.mark.asyncio
async def test_market_metrics_conflicting_duplicate_degrades_quality():
    service = MarketLayerService(MarketLayerConfig(), AsyncMock(), "metrics-test")
    service.metrics_quality.set_expected_symbols({"BTCUSDT"})
    service.rest_client.get_open_interest_history = AsyncMock(
        return_value=_open_interest()
    )
    service.rest_client.get_global_long_short_account_ratio = AsyncMock(
        return_value=_long_short()
    )
    service.metrics_store.publish = AsyncMock(
        side_effect=MetricsValidationError("conflicting duplicate metrics event")
    )

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr("trading_platform.market.main.time.time", lambda: NOW / 1000)
        await service._collect_metrics_symbol("BTCUSDT")

    quality = service.metrics_quality.snapshot(now_ms=NOW)["BTCUSDT"]
    assert quality["status"] == "degraded"
    assert quality["issue"] == "metrics_collection_failed:MetricsValidationError"
    await service.rest_client.close()


def test_metrics_subscription_is_accepted_and_exposed_as_required_quality():
    SubscriptionRequest(symbols=["BTCUSDT"], types=["metrics:5m"])
    app, service = create_app(MarketLayerConfig(), "metrics-health")
    service.redis.ping = AsyncMock(return_value=True)
    service.subscription_manager.update_subscription(
        "consumer", ["BTCUSDT"], ["metrics:5m"]
    )
    service._refresh_metrics_symbols(service.subscription_manager.get_active_streams())

    response = TestClient(app).get("/health")

    assert response.status_code == 503
    assert response.json()["metrics_quality_ready"] is False
    assert response.json()["metrics_quality_issues"] == 1
    assert service.websocket_required() is False
