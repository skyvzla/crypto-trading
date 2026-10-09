import asyncio
import json
from decimal import Decimal

import pytest

from trading_platform.market.recovery import (
    RecoveryError,
    recover_aggtrades,
    recover_klines,
)
from unittest.mock import AsyncMock
from trading_platform.market.main import MarketLayerConfig, MarketLayerService


def _trade(trade_id: int) -> dict:
    return {
        "a": trade_id, "p": "10.5", "q": "2", "f": trade_id,
        "l": trade_id, "T": trade_id * 1000, "m": False,
    }


def _kline(open_time: int) -> list:
    return [open_time, "1", "2", "0.5", "1.5", "3", open_time + 59_999]


def test_aggtrade_recovery_sorts_deduplicates_and_checks_continuity():
    result = recover_aggtrades([_trade(3), _trade(2), _trade(2), _trade(1)], expected_start_id=1, expected_end_id=3)
    assert [item["agg_trade_id"] for item in result] == [1, 2, 3]
    assert result[0]["price"] == Decimal("10.5")


def test_aggtrade_recovery_rejects_gap_and_limit_and_bad_row():
    with pytest.raises(RecoveryError, match="contains a gap"):
        recover_aggtrades([_trade(1), _trade(3)])
    with pytest.raises(RecoveryError, match="exceeds limit"):
        recover_aggtrades([_trade(1), _trade(2)], max_items=1)
    with pytest.raises(RecoveryError, match="missing"):
        recover_aggtrades([{"a": 1}])


def test_aggtrade_recovery_requires_known_post_reconnect_end():
    with pytest.raises(RecoveryError, match="ends with a gap"):
        recover_aggtrades(
            [_trade(101), _trade(102)],
            expected_start_id=101,
            expected_end_id=4999,
        )


def test_kline_recovery_sorts_deduplicates_filters_open_candle_and_checks_gap():
    rows = [_kline(60_000), _kline(0), _kline(0), _kline(120_000)]
    result = recover_klines(rows, "BTCUSDT", "1m", now_ms=180_000)
    assert [item.open_time for item in result] == [0, 60_000, 120_000]
    assert result[0].available_time == 60_000
    with pytest.raises(RecoveryError, match="contains a gap"):
        recover_klines([_kline(0), _kline(120_000)], "BTCUSDT", "1m")


def test_kline_recovery_rejects_limit_and_invalid_interval():
    with pytest.raises(RecoveryError, match="exceeds limit"):
        recover_klines([_kline(0), _kline(60_000)], "BTCUSDT", "1m", max_items=1)
    with pytest.raises(RecoveryError, match="unsupported interval"):
        recover_klines([], "BTCUSDT", "3x")


def test_degraded_kline_does_not_recover_from_in_progress_update():
    from trading_platform.market.quality import MarketDataQualityTracker

    tracker = MarketDataQualityTracker()
    stream = "btcusdt@kline_1m"
    tracker.begin_connection([stream], generation=1)
    tracker.mark_backfill_failed([stream])
    assert not tracker.observe_kline_update("BTCUSDT", "1m", 999, 1000)
    assert tracker.snapshot()[stream]["status"] == "degraded"


@pytest.mark.asyncio
async def test_service_backfills_aggtrade_before_post_reconnect_message():
    service = MarketLayerService(MarketLayerConfig(), AsyncMock(), "recovery-test")
    stream = "btcusdt@aggTrade"
    service._current_streams = [stream]
    service._quality_generation = 1
    service.ws_client.connection_generation = 2
    service.quality.begin_connection([stream], generation=1)
    assert service.quality.observe_aggtrade("BTCUSDT", 10, 10_000, 10_001)
    service.rest_client.get_agg_trades = AsyncMock(return_value=[_trade(11)])
    service.aggregator.add_trade = lambda **kwargs: []

    await service._recover_if_generation_changed()

    assert service.quality.snapshot()[stream]["status"] == "awaiting_data"
    assert service.quality.watermarks()[stream][0] == 11
    service.rest_client.get_agg_trades.assert_awaited_once_with(
        "BTCUSDT", from_id=11, limit=1000
    )

    await service._handle_ws_message({
        "e": "aggTrade", "s": "BTCUSDT", "a": 11, "p": "10.5", "q": "2",
        "f": 11, "l": 11, "T": 11_000, "m": False,
    })
    assert service.quality.snapshot()[stream]["status"] == "awaiting_data"

    await service._handle_ws_message({
        "e": "aggTrade", "s": "BTCUSDT", "a": 12, "p": "10.5", "q": "2",
        "f": 12, "l": 12, "T": 12_000, "m": False,
    })
    assert service.quality.snapshot()[stream]["status"] == "healthy"
    assert service.quality.watermarks()[stream][0] == 12


@pytest.mark.asyncio
async def test_service_restores_persisted_watermark_after_restart():
    redis = AsyncMock()
    redis.hgetall.return_value = {b"btcusdt@aggTrade": b"42"}
    service = MarketLayerService(MarketLayerConfig(), redis, "restart-test")

    await service._restore_persisted_watermarks(["btcusdt@aggTrade"])

    assert service.quality.watermarks()["btcusdt@aggTrade"][0] == 42


@pytest.mark.parametrize("third_timestamp", [2000, 2100])
async def test_subscription_refresh_preserves_unfinished_trade_bucket(third_timestamp):
    """Changing another subscription must not replay the retained stream's bucket."""
    stored = {}
    published = []
    client = AsyncMock()
    client.hgetall.side_effect = lambda _key: dict(stored)
    client.hset.side_effect = lambda _key, field, value: stored.update({field: value})
    client.xadd.side_effect = lambda _key, fields, **_kwargs: published.append(
        json.loads(fields["data"])
    )
    client.publish.return_value = 1
    service = MarketLayerService(MarketLayerConfig(), client, "refresh-test")
    await service.rest_client.close()
    messages = asyncio.Queue()

    async def receive_messages():
        while True:
            yield await messages.get()
            messages.task_done()

    async def connect(_streams):
        service.ws_client.connection_generation += 1

    service.ws_client.connect = AsyncMock(side_effect=connect)
    service.ws_client.disconnect = AsyncMock()
    service.ws_client.receive_messages = receive_messages
    service.rest_client.get_agg_trades = AsyncMock(return_value=[])
    service.subscription_manager.update_subscription("one", ["BTCUSDT"], ["bar1s"])

    def trade(number, timestamp):
        return {"e": "aggTrade", "s": "BTCUSDT", "a": number,
                "p": "1", "q": "1", "f": number, "l": number,
                "T": timestamp, "m": False}

    try:
        await service.refresh_ws_streams()
        for item in [trade(1, 1000), trade(2, 2000), trade(3, third_timestamp)]:
            await messages.put(item)
        await asyncio.wait_for(messages.join(), 2)
        assert stored["btcusdt@aggTrade"] == "1"
        service.subscription_manager.update_subscription("two", ["BTCUSDT"], ["kline:1m"])
        await service.refresh_ws_streams()
        await messages.put(trade(4, 3000))
        await asyncio.wait_for(messages.join(), 2)
        bar = next(item for item in published if item["timestamp"] == 2000)
        assert Decimal(bar["volume"]) == 2
        assert bar["trade_count"] == 2
        assert service.quality.snapshot()["btcusdt@aggTrade"]["status"] == "healthy"
    finally:
        await service._stop_ws_task()


@pytest.mark.asyncio
async def test_service_backfill_validates_before_publishing_partial_page():
    service = MarketLayerService(MarketLayerConfig(), AsyncMock(), "bounded-test")
    stream = "btcusdt@aggTrade"
    service._current_streams = [stream]
    service._quality_generation = 1
    service.ws_client.connection_generation = 2
    service.quality.begin_connection([stream], generation=1)
    assert service.quality.observe_aggtrade("BTCUSDT", 100, 100_000, 100_001)
    service.rest_client.get_agg_trades = AsyncMock(
        return_value=[_trade(value) for value in range(101, 1101)]
    )
    service.redis_publisher.publish_bar1s = AsyncMock()

    await service._recover_if_generation_changed({
        "e": "aggTrade", "s": "BTCUSDT", "a": 5000,
        "p": "10.5", "q": "2", "f": 5000, "l": 5000,
        "T": 5_000_000, "m": False,
    })

    assert service.quality.snapshot()[stream]["status"] == "degraded"
    service.redis_publisher.publish_bar1s.assert_not_awaited()


@pytest.mark.asyncio
async def test_trade_side_effect_failure_restores_quality_watermark():
    service = MarketLayerService(MarketLayerConfig(), AsyncMock(), "rollback-test")
    stream = "btcusdt@aggTrade"
    service._current_streams = [stream]
    service.quality.begin_connection([stream], generation=1)
    service.aggregator.add_trade = lambda **kwargs: []
    service.aggregator.last_finalized_trade_id = lambda symbol: 7
    service.redis_publisher.publish_bar1s = AsyncMock(side_effect=RuntimeError("redis"))

    # Force the durable watermark write to fail after the trade is accepted.
    service._persist_watermark = AsyncMock(side_effect=RuntimeError("redis"))
    await service._handle_ws_message({
        "e": "aggTrade", "s": "BTCUSDT", "a": 7, "p": "1", "q": "1",
        "f": 7, "l": 7, "T": 1000, "m": False,
    })

    assert service.quality.watermarks()[stream][0] is None
    assert service.quality.snapshot()[stream]["status"] == "degraded"
