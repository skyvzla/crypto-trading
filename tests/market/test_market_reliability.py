import asyncio
import json
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from websockets.exceptions import WebSocketException

from trading_platform.market.feed.aggregator import Bar1sAggregator
from trading_platform.market.feed.binance_ws import BinanceWebSocketClient
from trading_platform.market.main import MarketLayerConfig, MarketLayerService
from trading_platform.market.quality import MarketDataQualityTracker
from trading_platform.market.store.kline_store import KlineStore
from trading_platform.market.store.redis_pub import RedisPublisher
from trading_platform.shared.events import Bar1s


def _bar(symbol: str = "BTCUSDT") -> Bar1s:
    return Bar1s(
        symbol=symbol,
        timestamp=1_000,
        available_time=2_000,
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100"),
        volume=Decimal("2"),
        trade_count=1,
        vwap=Decimal("100"),
    )


@pytest.mark.asyncio
async def test_redis_publisher_detects_and_reports_consumer_disconnect(caplog):
    redis_client = AsyncMock()
    redis_client.publish.side_effect = [0, 2]
    publisher = RedisPublisher(redis_client)

    with caplog.at_level("WARNING"):
        assert await publisher.publish_bar1s(_bar()) == 0
    state = publisher.delivery_snapshot()["bar1s:BTCUSDT"]
    assert state["status"] == "degraded"
    assert state["zero_subscriber_count"] == 1
    assert publisher.delivery_ready is False
    assert publisher.delivery_issue_count == 1
    assert "消费端断流" in caplog.text
    stream_call = redis_client.xadd.await_args
    assert stream_call.args[0] == "bar1s:stream:BTCUSDT"
    assert json.loads(stream_call.args[1]["data"])["symbol"] == "BTCUSDT"
    assert "first_aggregate_trade_id" not in json.loads(
        stream_call.args[1]["data"]
    )
    assert stream_call.kwargs == {
        "maxlen": RedisPublisher.STREAM_MAXLEN,
        "approximate": True,
    }

    await publisher.publish_bar1s(_bar())
    state = publisher.delivery_snapshot()["bar1s:BTCUSDT"]
    assert state["status"] == "healthy"
    assert state["last_subscriber_count"] == 2
    assert publisher.delivery_ready is True
    assert publisher.delivery_issue_count == 0


@pytest.mark.asyncio
async def test_redis_publisher_writes_replay_stream_before_pubsub():
    redis_client = AsyncMock()
    calls = []
    redis_client.xadd.side_effect = lambda *args, **kwargs: calls.append("stream")
    redis_client.publish.side_effect = lambda *args, **kwargs: calls.append("pubsub") or 1

    await RedisPublisher(redis_client).publish_bar1s(_bar())

    assert calls == ["stream", "pubsub"]


def test_aggregator_returns_every_bar_closed_by_one_trade():
    aggregator = Bar1sAggregator(window_tolerance_ms=5000)

    assert aggregator.add_trade("BTCUSDT", Decimal("10"), Decimal("1"), 1000) == []
    assert [bar.timestamp for bar in aggregator.add_trade(
        "BTCUSDT", Decimal("30"), Decimal("1"), 3000
    )] == [1000]

    # A tolerated late trade creates another old window. The next current trade
    # must emit both completed windows instead of dropping one of them.
    assert [bar.timestamp for bar in aggregator.add_trade(
        "BTCUSDT", Decimal("20"), Decimal("1"), 2000
    )] == []
    assert [bar.timestamp for bar in aggregator.add_trade(
        "BTCUSDT", Decimal("50"), Decimal("1"), 5000
    )] == [2000, 3000]


def test_bar1s_watermarks_are_serialized_and_old_payloads_remain_compatible():
    bar = _bar()
    old_payload = bar.to_dict()
    old_payload.pop("first_aggregate_trade_id")
    old_payload.pop("last_aggregate_trade_id")

    assert Bar1s.from_dict(old_payload).first_aggregate_trade_id is None
    assert Bar1s.from_dict(old_payload).last_aggregate_trade_id is None

    watermarked = Bar1s.from_dict(
        {
            **old_payload,
            "first_aggregate_trade_id": 101,
            "last_aggregate_trade_id": 103,
        }
    )
    assert Bar1s.from_json(watermarked.to_json()) == watermarked


def test_aggregator_emits_first_and_last_aggregate_trade_ids():
    aggregator = Bar1sAggregator()
    aggregator.add_trade(
        "BTCUSDT", Decimal("10"), Decimal("1"), 1_100, aggregate_trade_id=102
    )
    aggregator.add_trade(
        "BTCUSDT", Decimal("11"), Decimal("1"), 1_200, aggregate_trade_id=101
    )

    emitted = aggregator.add_trade(
        "BTCUSDT", Decimal("12"), Decimal("1"), 2_000, aggregate_trade_id=103
    )

    assert emitted[0].first_aggregate_trade_id == 101
    assert emitted[0].last_aggregate_trade_id == 102


def test_aggregator_preserves_taker_orderflow_and_raw_trade_counts():
    aggregator = Bar1sAggregator()
    aggregator.add_trade(
        "BTCUSDT",
        Decimal("10"),
        Decimal("2"),
        1_100,
        aggregate_trade_id=10,
        first_trade_id=100,
        last_trade_id=101,
        is_buyer_maker=False,
    )
    aggregator.add_trade(
        "BTCUSDT",
        Decimal("12"),
        Decimal("3"),
        1_200,
        aggregate_trade_id=11,
        first_trade_id=102,
        last_trade_id=104,
        is_buyer_maker=True,
    )
    emitted = aggregator.add_trade(
        "BTCUSDT",
        Decimal("11"),
        Decimal("1"),
        2_000,
        aggregate_trade_id=12,
        first_trade_id=105,
        last_trade_id=105,
        is_buyer_maker=False,
    )

    bar = emitted[0]
    assert bar.trade_count == 2
    assert bar.raw_trade_count == 5
    assert bar.quote_volume == Decimal("56")
    assert bar.vwap == Decimal("11.2")
    assert bar.taker_buy_volume == Decimal("2")
    assert bar.taker_sell_volume == Decimal("3")
    assert bar.taker_buy_trade_count == 2
    assert bar.taker_sell_trade_count == 3
    assert bar.taker_buy_agg_trade_count == 1
    assert bar.taker_sell_agg_trade_count == 1
    assert bar.volume_delta == Decimal("-1")
    assert bar.volume_imbalance == Decimal("-0.2")
    assert bar.quote_volume_delta == Decimal("-16")
    assert bar.quote_volume_imbalance == Decimal("-16") / Decimal("56")
    assert bar.taker_buy_vwap == Decimal("10")
    assert bar.taker_sell_vwap == Decimal("12")
    assert bar.avg_taker_buy_raw_trade_quantity == Decimal("1")
    assert bar.avg_taker_sell_raw_trade_quantity == Decimal("1")
    assert bar.first_trade_id == 100
    assert bar.last_trade_id == 104
    assert Bar1s.from_json(bar.to_json()) == bar


def test_aggregator_drops_trade_for_an_already_published_second():
    aggregator = Bar1sAggregator(window_tolerance_ms=5000)
    aggregator.add_trade("BTCUSDT", Decimal("10"), Decimal("1"), 1000)
    emitted = aggregator.add_trade("BTCUSDT", Decimal("30"), Decimal("1"), 3000)
    assert [bar.timestamp for bar in emitted] == [1000]

    assert aggregator.add_trade(
        "BTCUSDT", Decimal("11"), Decimal("1"), 1500
    ) == []
    emitted = aggregator.add_trade("BTCUSDT", Decimal("50"), Decimal("1"), 5000)
    assert [bar.timestamp for bar in emitted] == [3000]


def test_quality_tracker_rejects_duplicate_and_gap_without_guessing_recovery():
    tracker = MarketDataQualityTracker()
    stream = "btcusdt@aggTrade"
    tracker.begin_connection([stream], generation=1)

    assert tracker.observe_aggtrade("BTCUSDT", 100, 1_000, 2_000)
    assert not tracker.observe_aggtrade("BTCUSDT", 100, 1_000, 2_001)
    assert tracker.snapshot()[stream]["status"] == "healthy"
    assert tracker.snapshot()[stream]["duplicate_count"] == 1

    assert not tracker.observe_aggtrade("BTCUSDT", 102, 1_002, 2_002)
    quality = tracker.snapshot()[stream]
    assert quality["status"] == "degraded"
    assert quality["gap_count"] == 1
    assert not tracker.observe_aggtrade("BTCUSDT", 103, 1_003, 2_003)


def test_quality_tracker_detects_completed_kline_gap():
    tracker = MarketDataQualityTracker()
    stream = "btcusdt@kline_1m"
    tracker.begin_connection([stream], generation=1)

    assert tracker.observe_kline("BTCUSDT", "1m", 60_000, 119_999, 120_100)
    assert not tracker.observe_kline("BTCUSDT", "1m", 180_000, 239_999, 240_100)
    assert tracker.snapshot()[stream]["status"] == "degraded"


@pytest.mark.asyncio
async def test_in_progress_kline_proves_transport_ready_without_being_stored():
    service = MarketLayerService(MarketLayerConfig(), AsyncMock(), "test-epoch")
    stream = "btcusdt@kline_5m"
    service._current_streams = [stream]
    service.ws_client.connection_generation = 1
    service._quality_generation = 1
    service.quality.begin_connection([stream], generation=1)
    service.kline_store.store_kline = AsyncMock()

    await service._handle_ws_message(
        {
            "e": "kline",
            "E": 301_000,
            "s": "BTCUSDT",
            "k": {
                "i": "5m",
                "t": 300_000,
                "T": 599_999,
                "x": False,
            },
        }
    )

    assert service.quality.snapshot()[stream]["status"] == "healthy"
    service.kline_store.store_kline.assert_not_awaited()


@pytest.mark.asyncio
async def test_health_fails_closed_when_active_pubsub_has_no_consumers():
    from trading_platform.market.main import create_app

    app, service = create_app(MarketLayerConfig(), "test-epoch")
    service.redis.ping = AsyncMock(return_value=True)
    service.redis_publisher.redis.xadd = AsyncMock(return_value="1-0")
    service.redis_publisher.redis.publish = AsyncMock(return_value=0)
    await service.redis_publisher.publish_bar1s(_bar())
    service.subscription_manager.update_subscription(
        "consumer", ["BTCUSDT"], ["bar1s"]
    )
    service._current_streams = ["btcusdt@aggTrade"]

    response = TestClient(app).get("/health")

    assert response.status_code == 503
    assert response.json()["pubsub_delivery_ready"] is False
    assert response.json()["pubsub_delivery_issues"] == 1


def test_historical_kline_http_stack_returns_completed_range():
    from trading_platform.market.main import create_app

    app, service = create_app(MarketLayerConfig(), "http-range-test")
    service.rest_client.get_klines = AsyncMock(return_value=[
        [0, "1", "2", "0.5", "1.5", "3", 59_999],
        [60_000, "1.5", "2", "1", "1.8", "4", 119_999],
    ])

    response = TestClient(app).get(
        "/klines/BTCUSDT/1m?start_time=0&end_time=120000&limit=2"
    )

    assert response.status_code == 200
    assert response.json()["source"] == "binance_rest"
    assert len(response.json()["klines"]) == 2


def test_bar1s_recovery_http_requires_a_closed_contiguous_id_range():
    from trading_platform.market.main import create_app

    app, service = create_app(MarketLayerConfig(), "bar-recovery-test")
    service.rest_client.get_agg_trades = AsyncMock(return_value=[
        {"a": 11, "p": "100", "q": "1", "f": 21, "l": 21, "T": 1_100, "m": True},
        {"a": 12, "p": "101", "q": "2", "f": 22, "l": 22, "T": 1_500, "m": False},
    ])

    response = TestClient(app).get(
        "/bar1s/BTCUSDT/recover?from_id=11&to_id=12"
    )

    assert response.status_code == 200
    assert response.json()["bars"][0]["first_aggregate_trade_id"] == 11
    assert response.json()["bars"][0]["last_aggregate_trade_id"] == 12
    service.rest_client.get_agg_trades.assert_awaited_once_with(
        "BTCUSDT", from_id=11, limit=2
    )


def test_bar1s_recovery_http_rejects_unclosed_or_oversized_ranges():
    from trading_platform.market.main import create_app

    app, service = create_app(MarketLayerConfig(), "bar-recovery-test")
    service.rest_client.get_agg_trades = AsyncMock(return_value=[])
    client = TestClient(app)

    assert client.get(
        "/bar1s/BTCUSDT/recover?from_id=11&to_id=12"
    ).status_code == 409
    assert client.get(
        "/bar1s/BTCUSDT/recover?from_id=1&to_id=1001"
    ).status_code == 400


@pytest.mark.asyncio
async def test_websocket_reconnects_with_the_current_streams(monkeypatch):
    first_socket = AsyncMock()
    first_socket.recv.side_effect = WebSocketException("connection lost")
    second_socket = AsyncMock()
    second_socket.recv.return_value = json.dumps(
        {"stream": "btcusdt@aggTrade", "data": {"e": "aggTrade", "s": "BTCUSDT"}}
    )
    connect = AsyncMock(side_effect=[first_socket, second_socket])
    monkeypatch.setattr("trading_platform.market.feed.binance_ws.websockets.connect", connect)

    client = BinanceWebSocketClient(
        ws_base_url="wss://example.invalid",
        reconnect_delay=0,
        max_reconnect_attempts=2,
    )
    await client.connect(["ethusdt@kline_1m", "btcusdt@aggTrade"])

    message = await anext(client.receive_messages())

    assert message == {"e": "aggTrade", "s": "BTCUSDT"}
    expected_url = (
        "wss://example.invalid/stream?streams="
        "btcusdt@aggTrade/ethusdt@kline_1m"
    )
    assert [call.args[0] for call in connect.await_args_list] == [expected_url, expected_url]
    await client.disconnect()


@pytest.mark.asyncio
async def test_refresh_builds_only_each_symbols_requested_streams():
    service = MarketLayerService(MarketLayerConfig(), AsyncMock(), "test-epoch")
    service.subscription_manager.get_active_streams = lambda: {
        "BTCUSDT": {"bar1s": 1},
        "ETHUSDT": {"kline:1m": 1},
    }
    service.ws_client = AsyncMock()
    keep_running = asyncio.Event()

    async def message_loop():
        await keep_running.wait()

    service._ws_message_loop = message_loop

    await service.refresh_ws_streams()

    service.ws_client.connect.assert_awaited_once_with(
        ["btcusdt@aggTrade", "ethusdt@kline_1m"]
    )
    await service.stop()


@pytest.mark.asyncio
async def test_refresh_restarts_a_failed_task_even_when_streams_are_unchanged():
    service = MarketLayerService(MarketLayerConfig(), AsyncMock(), "test-epoch")
    service.subscription_manager.get_active_streams = lambda: {
        "BTCUSDT": {"bar1s": 1},
    }
    service.ws_client = AsyncMock()

    async def fail():
        raise RuntimeError("message loop failed")

    failed_task = asyncio.create_task(fail())
    await asyncio.sleep(0)
    service._ws_task = failed_task
    service._current_streams = ["btcusdt@aggTrade"]
    keep_running = asyncio.Event()

    async def message_loop():
        await keep_running.wait()

    service._ws_message_loop = message_loop

    await service.refresh_ws_streams()

    service.ws_client.disconnect.assert_awaited_once()
    service.ws_client.connect.assert_awaited_once_with(["btcusdt@aggTrade"])
    assert service._ws_task is not failed_task
    assert not service._ws_task.done()
    await service.stop()


@pytest.mark.asyncio
async def test_market_service_publishes_all_completed_bars():
    service = MarketLayerService(MarketLayerConfig(), AsyncMock(), "test-epoch")
    bars = [AsyncMock(timestamp=1000), AsyncMock(timestamp=2000)]
    service.aggregator.add_trade = lambda **kwargs: bars
    service.redis_publisher.publish_bar1s = AsyncMock()

    await service._handle_aggtrade(
        "BTCUSDT",
        {"price": Decimal("10"), "quantity": Decimal("1"), "timestamp": 3000},
    )

    assert [call.args[0] for call in service.redis_publisher.publish_bar1s.await_args_list] == bars


def test_health_reports_redis_failure_as_not_ready():
    from trading_platform.market.main import create_app

    app, service = create_app(MarketLayerConfig(), "test-epoch")
    service.redis.ping = AsyncMock(side_effect=ConnectionError("redis unavailable"))

    response = TestClient(app).get("/health")

    assert response.status_code == 503
    assert response.json()["status"] == "degraded"
    assert response.json()["redis_connected"] is False


def test_health_is_ready_without_subscriptions_when_redis_is_available():
    from trading_platform.market.main import create_app

    app, service = create_app(MarketLayerConfig(), "test-epoch")
    service.redis.ping = AsyncMock(return_value=True)

    response = TestClient(app).get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    assert response.json()["websocket_connected"] is True


def test_health_exposes_testnet_environment(monkeypatch):
    from trading_platform.market.main import create_app

    monkeypatch.setenv("BINANCE_TESTNET", "true")
    app, service = create_app(MarketLayerConfig(), "test-epoch")
    service.redis.ping = AsyncMock(return_value=True)

    response = TestClient(app).get("/health")

    assert response.status_code == 200
    assert response.json()["binance_testnet"] is True
    assert service.ws_client.ws_base_url == "wss://stream.binancefuture.com"


def test_health_reports_required_websocket_failure_as_not_ready():
    from trading_platform.market.main import create_app

    app, service = create_app(MarketLayerConfig(), "test-epoch")
    service.redis.ping = AsyncMock(return_value=True)
    service._current_streams = ["btcusdt@aggTrade"]

    response = TestClient(app).get("/health")

    assert response.status_code == 503
    assert response.json()["status"] == "degraded"
    assert response.json()["websocket_connected"] is False


def test_health_reports_stream_quality_gate():
    from trading_platform.market.main import create_app

    app, service = create_app(MarketLayerConfig(), "test-epoch")
    service.redis.ping = AsyncMock(return_value=True)
    service.quality.begin_connection(["btcusdt@aggTrade"], generation=1)

    response = TestClient(app).get("/health")

    assert response.status_code == 503
    assert response.json()["data_quality_ready"] is False
    assert response.json()["data_quality_issues"] == 1


@pytest.mark.asyncio
async def test_message_task_failure_is_recovered_with_existing_subscription():
    service = MarketLayerService(MarketLayerConfig(), AsyncMock(), "test-epoch")
    service._running = True
    service.subscription_manager.update_subscription(
        "consumer", ["BTCUSDT"], ["bar1s"]
    )
    service.ws_client = AsyncMock()
    service.ws_client.reconnect_delay = 0
    service.ws_client.connection_generation = 1
    service.ws_client.connect.side_effect = [None]
    service.ws_client.connected = True

    async def receive_messages():
        await asyncio.Event().wait()
        yield {}

    service.ws_client.receive_messages = receive_messages
    service._current_streams = ["btcusdt@aggTrade"]

    async def fail():
        raise RuntimeError("message loop failed")

    failed = asyncio.create_task(fail())
    await asyncio.sleep(0)
    service._ws_task = failed
    service._on_ws_task_done(failed)
    await asyncio.sleep(0.01)

    service.ws_client.connect.assert_awaited_once_with(["btcusdt@aggTrade"])
    assert service._ws_task is not None
    await service.stop()


@pytest.mark.asyncio
async def test_market_service_closes_shared_redis_with_aclose():
    redis_client = AsyncMock()
    service = MarketLayerService(MarketLayerConfig(), redis_client, "test-epoch")
    service.ws_client = AsyncMock()

    await service.stop()

    redis_client.aclose.assert_awaited_once_with()
    redis_client.close.assert_not_called()


@pytest.mark.asyncio
async def test_store_close_helpers_use_aclose():
    publisher_redis = AsyncMock()
    store_redis = AsyncMock()

    await RedisPublisher(publisher_redis).close()
    await KlineStore(store_redis).close()

    publisher_redis.aclose.assert_awaited_once_with()
    publisher_redis.close.assert_not_called()
    store_redis.aclose.assert_awaited_once_with()
    store_redis.close.assert_not_called()
