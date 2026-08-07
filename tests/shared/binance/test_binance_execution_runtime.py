import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from trading_platform.shared.binance.runtime import BinanceExecutionRuntime


def _runtime():
    stream = Mock(start=AsyncMock(), stop=AsyncMock(), on_reconnect=None)
    resolver = Mock(resolve_recovered_unknowns_once=AsyncMock(return_value={}))

    async def idle():
        await asyncio.Future()

    poller = Mock(
        resolver=resolver,
        start=Mock(side_effect=lambda: asyncio.create_task(idle())),
        stop=AsyncMock(),
    )
    return BinanceExecutionRuntime(stream, poller), stream, poller, resolver


@pytest.mark.asyncio
async def test_runtime_starts_stream_then_reconciles_and_starts_poller():
    calls = []
    runtime, stream, poller, resolver = _runtime()
    stream.start.side_effect = lambda: calls.append("stream")
    resolver.resolve_recovered_unknowns_once.side_effect = (
        lambda: calls.append("reconcile") or {}
    )
    poller.start.side_effect = lambda: calls.append("poller")

    await runtime.start()
    await runtime.start()

    assert calls == ["stream", "reconcile", "poller"]
    assert runtime.is_running is True


@pytest.mark.asyncio
async def test_runtime_start_failure_cleans_up_stream_and_poller():
    runtime, stream, poller, resolver = _runtime()
    resolver.resolve_recovered_unknowns_once.side_effect = RuntimeError("query failed")

    with pytest.raises(RuntimeError, match="query failed"):
        await runtime.start()

    poller.stop.assert_awaited_once()
    stream.stop.assert_awaited_once()
    assert runtime.is_running is False


@pytest.mark.asyncio
async def test_runtime_stop_closes_stream_before_poller():
    calls = []
    runtime, stream, poller, _ = _runtime()
    stream.stop.side_effect = lambda: calls.append("stream")
    poller.stop.side_effect = lambda: calls.append("poller")

    await runtime.stop()

    assert calls == ["stream", "poller"]


@pytest.mark.asyncio
async def test_runtime_reconnect_restarts_recovery_and_preserves_callback():
    previous = AsyncMock()
    runtime, stream, poller, _ = _runtime()
    runtime._previous_reconnect = previous

    await stream.on_reconnect()

    poller.stop.assert_awaited_once()
    poller.start.assert_called_once()
    previous.assert_awaited_once()


@pytest.mark.asyncio
async def test_runtime_runs_startup_reconciler_before_poller():
    runtime, stream, poller, resolver = _runtime()
    reconciler = Mock(reconcile_once=AsyncMock())
    runtime = BinanceExecutionRuntime(stream, poller, reconciler)
    calls = []
    resolver.resolve_recovered_unknowns_once.side_effect = (
        lambda: calls.append("unknown") or {}
    )
    reconciler.reconcile_once.side_effect = lambda: calls.append("snapshot")
    poller.start.side_effect = lambda: calls.append("poller")

    await runtime.start()

    assert calls == ["unknown", "snapshot", "poller"]


@pytest.mark.asyncio
async def test_runtime_reconnect_reconciles_before_restarting_poller():
    runtime, stream, poller, _ = _runtime()
    reconciler = Mock(reconcile_once=AsyncMock())
    runtime = BinanceExecutionRuntime(stream, poller, reconciler)
    runtime._previous_reconnect = None
    calls = []
    poller.stop.side_effect = lambda: calls.append("stop-poller")
    reconciler.reconcile_once.side_effect = lambda: calls.append("snapshot")
    poller.start.side_effect = lambda: calls.append("poller")

    await stream.on_reconnect()

    assert calls == ["stop-poller", "snapshot", "poller"]


@pytest.mark.asyncio
async def test_runtime_marks_recovered_only_after_reconciliation_and_poller_restart():
    runtime, stream, poller, _ = _runtime()
    reconciler = Mock(reconcile_once=AsyncMock())
    recovered = Mock()
    runtime = BinanceExecutionRuntime(
        stream, poller, reconciler, on_recovered=recovered
    )
    runtime._previous_reconnect = None
    calls = []
    poller.stop.side_effect = lambda: calls.append("stop-poller")
    reconciler.reconcile_once.side_effect = lambda: calls.append("snapshot")
    poller.start.side_effect = lambda: calls.append("poller")
    recovered.side_effect = lambda: calls.append("recovered")

    await stream.on_reconnect()

    assert calls == ["stop-poller", "snapshot", "poller", "recovered"]


@pytest.mark.asyncio
async def test_runtime_reconnect_recovery_failure_keeps_poller_stopped():
    previous = AsyncMock()
    runtime, stream, poller, _ = _runtime()
    reconciler = Mock(
        reconcile_once=AsyncMock(side_effect=RuntimeError("recovery mismatch"))
    )
    runtime = BinanceExecutionRuntime(stream, poller, reconciler)
    runtime._previous_reconnect = previous

    with pytest.raises(RuntimeError, match="recovery mismatch"):
        await stream.on_reconnect()

    poller.stop.assert_awaited_once()
    poller.start.assert_not_called()
    previous.assert_not_awaited()


@pytest.mark.asyncio
async def test_runtime_reconciliation_failure_is_fail_closed():
    runtime, stream, poller, _ = _runtime()
    reconciler = Mock(
        reconcile_once=AsyncMock(side_effect=RuntimeError("state mismatch"))
    )
    runtime = BinanceExecutionRuntime(stream, poller, reconciler)

    with pytest.raises(RuntimeError, match="state mismatch"):
        await runtime.start()

    poller.stop.assert_awaited_once()
    stream.stop.assert_awaited_once()
    assert runtime.is_running is False
