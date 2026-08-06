import asyncio
import json
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from websockets.exceptions import WebSocketException

from trading_platform.market.feed.aggregator import Bar1sAggregator
from trading_platform.market.feed.binance_ws import BinanceWebSocketClient
from trading_platform.market.main import MarketLayerConfig, MarketLayerService


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
