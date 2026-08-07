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


async def _idle_forever(*_args):
    await asyncio.Future()


@pytest.mark.asyncio
async def test_start_waits_until_websocket_is_really_open(monkeypatch):
    monkeypatch.setattr(
        "trading_platform.shared.binance.user_stream.websocket.WebSocketApp",
        FakeWebSocketApp,
    )
    rest = Mock(
        create_listen_key=AsyncMock(return_value="listen-key"),
        close_listen_key=AsyncMock(),
    )
    stream = UserDataStream(rest)
    stream._run_ws = AsyncMock(side_effect=_idle_forever)

    start = asyncio.create_task(stream.start())
    await asyncio.sleep(0)

    assert start.done() is False
    FakeWebSocketApp.instance.callbacks["on_open"](FakeWebSocketApp.instance)
    await asyncio.wait_for(start, timeout=1)
    assert stream.connected is True

    await stream.stop()
    assert stream.connected is False


@pytest.mark.asyncio
async def test_reconnect_callback_runs_only_after_new_websocket_is_open(monkeypatch):
    monkeypatch.setattr(
        "trading_platform.shared.binance.user_stream.websocket.WebSocketApp",
        FakeWebSocketApp,
    )
    rest = Mock(
        create_listen_key=AsyncMock(return_value="new-listen-key"),
        close_listen_key=AsyncMock(),
    )
    recovered = AsyncMock()
    stream = UserDataStream(rest, on_reconnect=recovered)
    stream._loop = asyncio.get_running_loop()
    stream._running = True
    stream.listen_key = "old-listen-key"
    stream._reconnect_delay = 0
    stream._run_ws = AsyncMock(side_effect=_idle_forever)

    reconnect = asyncio.create_task(stream._reconnect())
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    recovered.assert_not_awaited()
    FakeWebSocketApp.instance.callbacks["on_open"](FakeWebSocketApp.instance)
    await asyncio.wait_for(reconnect, timeout=1)
    recovered.assert_awaited_once()

    await stream.stop()


@pytest.mark.asyncio
async def test_reconnect_remains_disconnected_across_repeated_failures(monkeypatch):
    monkeypatch.setattr(
        "trading_platform.shared.binance.user_stream.websocket.WebSocketApp",
        FakeWebSocketApp,
    )
    rest = Mock(
        create_listen_key=AsyncMock(
            side_effect=[
                RuntimeError("network down 1"),
                RuntimeError("network down 2"),
                "recovered-listen-key",
            ]
        ),
        close_listen_key=AsyncMock(),
    )
    disconnected = AsyncMock()
    recovered = AsyncMock()
    stream = UserDataStream(
        rest,
        on_disconnect=disconnected,
        on_reconnect=recovered,
    )
    stream._loop = asyncio.get_running_loop()
    stream._running = True
    stream.listen_key = "stale-listen-key"
    stream._connected_event = asyncio.Event()
    stream._connected_event.set()
    stream._mark_disconnected()
    stream._reconnect_delay = 0
    stream._max_reconnect_delay = 0
    stream._run_ws = AsyncMock(side_effect=_idle_forever)

    reconnect = asyncio.create_task(stream._reconnect_after_disconnect())
    while rest.create_listen_key.await_count < 3:
        await asyncio.sleep(0)

    assert stream.connected is False
    assert reconnect.done() is False
    recovered.assert_not_awaited()
    FakeWebSocketApp.instance.callbacks["on_open"](FakeWebSocketApp.instance)
    await asyncio.wait_for(reconnect, timeout=1)

    disconnected.assert_awaited_once()
    recovered.assert_awaited_once()
    assert stream.connected is True
    await stream.stop()


@pytest.mark.asyncio
async def test_reconnect_exhaustion_sets_fatal_and_remains_disconnected():
    rest = Mock(
        create_listen_key=AsyncMock(side_effect=RuntimeError("network down")),
        close_listen_key=AsyncMock(),
    )
    recovered = AsyncMock()
    stream = UserDataStream(
        rest,
        on_reconnect=recovered,
        max_reconnect_attempts=2,
    )
    stream._loop = asyncio.get_running_loop()
    stream._running = True
    stream.listen_key = "stale-listen-key"
    stream._connected_event = asyncio.Event()
    stream._reconnect_delay = 0
    stream._max_reconnect_delay = 0

    await stream._reconnect()

    failure = await asyncio.wait_for(stream.wait_fatal(), timeout=1)
    assert str(failure) == "User Data Stream reconnect attempts exhausted: 2"
    assert rest.create_listen_key.await_count == 2
    assert stream.connected is False
    recovered.assert_not_awaited()
    await stream.stop()


@pytest.mark.asyncio
async def test_reconnect_cleans_failed_websocket_before_next_attempt():
    rest = Mock(
        create_listen_key=AsyncMock(side_effect=["key-1", "key-2"]),
        close_listen_key=AsyncMock(),
    )
    stream = UserDataStream(rest)
    stream._loop = asyncio.get_running_loop()
    stream._running = True
    stream._reconnect_delay = 0
    stream._max_reconnect_delay = 0
    failed_ws = Mock(close=Mock())
    failed_task = None
    attempts = 0

    async def connect_ws():
        nonlocal attempts, failed_task
        attempts += 1
        stream.ws = failed_ws if attempts == 1 else Mock(close=Mock())
        stream._ws_thread = asyncio.create_task(_idle_forever())
        if attempts == 1:
            failed_task = stream._ws_thread

    async def wait_until_connected():
        if attempts == 1:
            raise TimeoutError("open timed out")

    stream._connect_ws = AsyncMock(side_effect=connect_ws)
    stream._wait_until_connected = AsyncMock(side_effect=wait_until_connected)

    await asyncio.wait_for(stream._reconnect(), timeout=1)

    assert attempts == 2
    failed_ws.close.assert_called_once()
    assert failed_task is not None and failed_task.cancelled()
    await stream.stop()


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
            FakeWebSocketApp.instance,
            '{"e":"ORDER_TRADE_UPDATE","o":{"c":"client-1"}}',
        )
    )
    thread.start()
    thread.join()
    await asyncio.wait_for(received.wait(), timeout=1)
    stream._ws_thread.cancel()
    await stream._ws_thread


@pytest.mark.asyncio
async def test_account_updates_are_returned_to_event_loop_as_complete_events(monkeypatch):
    monkeypatch.setattr(
        "trading_platform.shared.binance.user_stream.websocket.WebSocketApp",
        FakeWebSocketApp,
    )
    rest = Mock(create_listen_key=AsyncMock(return_value="listen-key"))
    received = asyncio.Event()

    async def on_account_update(event):
        assert event["e"] == "ACCOUNT_UPDATE"
        assert event["T"] == 1780000000000
        assert event["a"]["P"][0]["s"] == "BTCUSDT"
        received.set()

    stream = UserDataStream(rest, on_account_update=on_account_update)
    stream._loop = asyncio.get_running_loop()
    stream._running = True
    stream.listen_key = "listen-key"
    stream._run_ws = AsyncMock()
    await stream._connect_ws()

    message = (
        '{"e":"ACCOUNT_UPDATE","E":1780000000100,"T":1780000000000,'
        '"a":{"m":"ORDER","B":[],"P":[{"s":"BTCUSDT"}]}}'
    )
    thread = threading.Thread(
        target=lambda: FakeWebSocketApp.instance.callbacks["on_message"](
            FakeWebSocketApp.instance, message
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

    on_close(FakeWebSocketApp.instance, 1006, "closed")
    first = stream._reconnect_task
    on_close(FakeWebSocketApp.instance, 1006, "closed again")
    assert stream._reconnect_task is first
    await asyncio.wait_for(reconnect_started.wait(), timeout=1)
    assert stream._reconnect.await_count == 1

    await stream.stop()
    assert first.cancelled()
    on_close(FakeWebSocketApp.instance, 1006, "after stop")
    assert stream._reconnect_task is None


@pytest.mark.asyncio
async def test_stale_websocket_close_does_not_disconnect_or_reconnect(monkeypatch):
    monkeypatch.setattr(
        "trading_platform.shared.binance.user_stream.websocket.WebSocketApp",
        FakeWebSocketApp,
    )
    rest = Mock(create_listen_key=AsyncMock(return_value="listen-key"))
    stream = UserDataStream(rest)
    stream._loop = asyncio.get_running_loop()
    stream._running = True
    stream.listen_key = "listen-key"
    stream._connected_event = asyncio.Event()
    stream._run_ws = AsyncMock(side_effect=_idle_forever)

    await stream._connect_ws()
    stale_ws = stream.ws
    stale_on_close = stale_ws.callbacks["on_close"]
    await stream._close_ws_connection()
    await stream._connect_ws()
    current_ws = stream.ws
    current_ws.callbacks["on_open"](current_ws)
    await asyncio.sleep(0)

    stale_on_close(stale_ws, 1006, "late close")
    await asyncio.sleep(0)

    assert stream.ws is current_ws
    assert stream.connected is True
    assert stream._reconnect_task is None
    await stream.stop()


@pytest.mark.asyncio
async def test_stale_websocket_task_done_does_not_reconnect():
    rest = Mock(create_listen_key=AsyncMock(return_value="listen-key"))
    stream = UserDataStream(rest)
    stream._loop = asyncio.get_running_loop()
    stream._running = True
    stream._connected_event = asyncio.Event()
    stream._connected_event.set()
    stale_task = asyncio.create_task(asyncio.sleep(0))
    await stale_task
    current_task = asyncio.create_task(_idle_forever())
    stream._ws_thread = current_task

    stream._ws_task_done(stale_task)

    assert stream.connected is True
    assert stream._reconnect_task is None
    current_task.cancel()
    await asyncio.gather(current_task, return_exceptions=True)


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


@pytest.mark.asyncio
async def test_callback_failure_sets_fatal_signal(monkeypatch):
    monkeypatch.setattr(
        "trading_platform.shared.binance.user_stream.websocket.WebSocketApp",
        FakeWebSocketApp,
    )
    rest = Mock(create_listen_key=AsyncMock(return_value="listen-key"))

    async def fail_report(_order):
        raise RuntimeError("ledger unavailable")

    stream = UserDataStream(rest, on_execution_report=fail_report)
    stream._loop = asyncio.get_running_loop()
    stream._running = True
    stream.listen_key = "listen-key"
    stream._run_ws = AsyncMock()
    await stream._connect_ws()

    FakeWebSocketApp.instance.callbacks["on_message"](
        FakeWebSocketApp.instance,
        '{"e":"ORDER_TRADE_UPDATE","o":{"c":"client-1"}}',
    )

    failure = await asyncio.wait_for(stream.wait_fatal(), timeout=1)
    assert isinstance(failure, RuntimeError)
    assert str(failure) == "ledger unavailable"
    await stream.stop()


@pytest.mark.asyncio
async def test_malformed_execution_message_sets_fatal_signal(monkeypatch):
    monkeypatch.setattr(
        "trading_platform.shared.binance.user_stream.websocket.WebSocketApp",
        FakeWebSocketApp,
    )
    rest = Mock(create_listen_key=AsyncMock(return_value="listen-key"))
    stream = UserDataStream(rest)
    stream._loop = asyncio.get_running_loop()
    stream._running = True
    stream.listen_key = "listen-key"
    stream._run_ws = AsyncMock(side_effect=_idle_forever)
    await stream._connect_ws()

    FakeWebSocketApp.instance.callbacks["on_message"](
        FakeWebSocketApp.instance, "not-json"
    )

    failure = await asyncio.wait_for(stream.wait_fatal(), timeout=1)
    assert isinstance(failure, ValueError)
    await stream.stop()


@pytest.mark.asyncio
async def test_stop_waits_for_in_flight_callback_within_drain_timeout(monkeypatch):
    monkeypatch.setattr(
        "trading_platform.shared.binance.user_stream.websocket.WebSocketApp",
        FakeWebSocketApp,
    )
    rest = Mock(
        create_listen_key=AsyncMock(return_value="listen-key"),
        close_listen_key=AsyncMock(),
    )
    callback_started = asyncio.Event()
    release_callback = asyncio.Event()

    async def delayed_report(_order):
        callback_started.set()
        await release_callback.wait()

    stream = UserDataStream(
        rest,
        on_execution_report=delayed_report,
        callback_drain_timeout_seconds=1,
    )
    stream._loop = asyncio.get_running_loop()
    stream._running = True
    stream.listen_key = "listen-key"
    stream._run_ws = AsyncMock()
    await stream._connect_ws()
    FakeWebSocketApp.instance.callbacks["on_message"](
        FakeWebSocketApp.instance,
        '{"e":"ORDER_TRADE_UPDATE","o":{"c":"client-1"}}',
    )
    await asyncio.wait_for(callback_started.wait(), timeout=1)

    stopping = asyncio.create_task(stream.stop())
    await asyncio.sleep(0)
    assert stopping.done() is False
    release_callback.set()
    await asyncio.wait_for(stopping, timeout=1)
    assert stream._scheduled_futures == set()


@pytest.mark.asyncio
async def test_stop_cancels_callback_after_bounded_drain_timeout(monkeypatch):
    monkeypatch.setattr(
        "trading_platform.shared.binance.user_stream.websocket.WebSocketApp",
        FakeWebSocketApp,
    )
    rest = Mock(
        create_listen_key=AsyncMock(return_value="listen-key"),
        close_listen_key=AsyncMock(),
    )
    callback_started = asyncio.Event()
    callback_cancelled = asyncio.Event()

    async def blocked_report(_order):
        callback_started.set()
        try:
            await asyncio.Future()
        finally:
            callback_cancelled.set()

    stream = UserDataStream(
        rest,
        on_execution_report=blocked_report,
        callback_drain_timeout_seconds=0.01,
    )
    stream._loop = asyncio.get_running_loop()
    stream._running = True
    stream.listen_key = "listen-key"
    stream._run_ws = AsyncMock()
    await stream._connect_ws()
    FakeWebSocketApp.instance.callbacks["on_message"](
        FakeWebSocketApp.instance,
        '{"e":"ORDER_TRADE_UPDATE","o":{"c":"client-1"}}',
    )
    await asyncio.wait_for(callback_started.wait(), timeout=1)

    with pytest.raises(TimeoutError, match="timed out draining 1"):
        await asyncio.wait_for(stream.stop(), timeout=1)
    await asyncio.wait_for(callback_cancelled.wait(), timeout=1)
    assert stream._scheduled_futures == set()
    assert stream.listen_key is None
    rest.close_listen_key.assert_awaited_once_with("listen-key")
