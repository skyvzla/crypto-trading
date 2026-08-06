import asyncio
import threading
from unittest.mock import AsyncMock, Mock

import pytest

from trading_platform.shared.binance.user_stream import UserDataStream


class FakeWebSocketApp:
    instance = None

    def __init__(self, url, **callbacks):
        self.url = url
        self.callbacks = callbacks
        self.closed = False
        FakeWebSocketApp.instance = self

    def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_callbacks_from_websocket_thread_are_returned_to_event_loop(monkeypatch):
    monkeypatch.setattr(
        "trading_platform.shared.binance.user_stream.websocket.WebSocketApp",
        FakeWebSocketApp,
    )
    rest = Mock(create_listen_key=AsyncMock(return_value="listen-key"))
    received = asyncio.Event()

    async def on_report(order):
        assert order["c"] == "client-1"
        received.set()

    stream = UserDataStream(rest, on_execution_report=on_report)
    stream._loop = asyncio.get_running_loop()
    stream._running = True
    stream.listen_key = "listen-key"
    stream._run_ws = AsyncMock()
    await stream._connect_ws()

    thread = threading.Thread(
        target=lambda: FakeWebSocketApp.instance.callbacks["on_message"](
            None, '{"e":"ORDER_TRADE_UPDATE","o":{"c":"client-1"}}'
        )
    )
    thread.start()
    thread.join()
    await asyncio.wait_for(received.wait(), timeout=1)
    stream._ws_thread.cancel()
    await stream._ws_thread


@pytest.mark.asyncio
async def test_close_schedules_only_one_reconnect_and_stop_cancels_it(monkeypatch):
    monkeypatch.setattr(
        "trading_platform.shared.binance.user_stream.websocket.WebSocketApp",
        FakeWebSocketApp,
    )
    rest = Mock(create_listen_key=AsyncMock(return_value="listen-key"))
    stream = UserDataStream(rest)
    stream._loop = asyncio.get_running_loop()
    stream._running = True
    stream.listen_key = "listen-key"
    reconnect_started = asyncio.Event()

    async def reconnect():
        reconnect_started.set()
        await asyncio.Future()

    stream._reconnect = AsyncMock(side_effect=reconnect)
    stream._run_ws = AsyncMock()
    await stream._connect_ws()
    on_close = FakeWebSocketApp.instance.callbacks["on_close"]

    on_close(None, 1006, "closed")
    first = stream._reconnect_task
    on_close(None, 1006, "closed again")
    assert stream._reconnect_task is first
    await asyncio.wait_for(reconnect_started.wait(), timeout=1)
    assert stream._reconnect.await_count == 1

    await stream.stop()
    assert first.cancelled()
    on_close(None, 1006, "after stop")
    assert stream._reconnect_task is None


@pytest.mark.asyncio
async def test_start_failure_cleans_listen_key_and_background_tasks():
    rest = Mock(
        create_listen_key=AsyncMock(return_value="listen-key"),
        close_listen_key=AsyncMock(),
    )
    stream = UserDataStream(rest)
    stream._connect_ws = AsyncMock(side_effect=RuntimeError("ws unavailable"))

    with pytest.raises(RuntimeError, match="ws unavailable"):
        await stream.start()

    assert stream._running is False
    assert stream.listen_key is None
    assert stream._keepalive_task is None
    rest.close_listen_key.assert_awaited_once_with("listen-key")
