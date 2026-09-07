import asyncio
from dataclasses import dataclass
from unittest.mock import AsyncMock, Mock

import pytest

from trading_platform.shared.binance.runtime import BinanceExecutionRuntime


@dataclass(frozen=True)
class ReconciliationResult:
    open_order_count: int
    position_count: int


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


@pytest.mark.asyncio
async def test_runtime_startup_reconciliation_observer_is_fifo_and_keeps_trace():
    stream = Mock(start=AsyncMock(), stop=AsyncMock(), on_reconnect=None)
    poller = Mock(
        resolver=Mock(resolve_recovered_unknowns_once=AsyncMock(return_value={})),
        start=Mock(),
        stop=AsyncMock(),
    )
    result = ReconciliationResult(open_order_count=2, position_count=1)
    reconciler = Mock(reconcile_once=AsyncMock(return_value=result))
    observed = []

    async def observer(stage, details):
        observed.append((stage, details))

    runtime = BinanceExecutionRuntime(
        stream,
        poller,
        reconciler,
        reconciliation_observer=observer,
    )

    await runtime.start()

    assert [stage for stage, _ in observed] == ["started", "completed"]
    assert observed[0][1]["reason"] == "startup"
    assert observed[1][1]["reason"] == "startup"
    assert observed[0][1]["trace_id"] == observed[1][1]["trace_id"]
    assert observed[1][1]["result_type"] == "ReconciliationResult"
    assert observed[1][1]["result_fields"] == {
        "open_order_count": 2,
        "position_count": 1,
    }
    reconciler.reconcile_once.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_runtime_reconnect_reconciliation_observer_is_fifo_and_keeps_trace():
    stream = Mock(start=AsyncMock(), stop=AsyncMock(), on_reconnect=None)
    poller = Mock(
        resolver=Mock(resolve_recovered_unknowns_once=AsyncMock(return_value={})),
        start=Mock(),
        stop=AsyncMock(),
    )
    reconciler = Mock(reconcile_once=AsyncMock(return_value={"open_orders": 0}))
    observed = []

    def observer(stage, details):
        observed.append((stage, details))

    runtime = BinanceExecutionRuntime(
        stream,
        poller,
        reconciler,
        reconciliation_observer=observer,
    )
    runtime._previous_reconnect = None

    await stream.on_reconnect()

    assert [stage for stage, _ in observed] == ["started", "completed"]
    assert observed[0][1]["reason"] == "reconnect"
    assert observed[1][1]["reason"] == "reconnect"
    assert observed[0][1]["trace_id"] == observed[1][1]["trace_id"]
    assert observed[1][1]["result_type"] == "dict"
    assert observed[1][1]["result_fields"] == {"open_orders": 0}


@pytest.mark.asyncio
async def test_runtime_manual_reconciliation_failure_is_observed_with_same_trace():
    stream = Mock(start=AsyncMock(), stop=AsyncMock(), on_reconnect=None)
    poller = Mock(resolver=Mock(resolve_recovered_unknowns_once=AsyncMock()))
    reconciler = Mock(
        reconcile_once=AsyncMock(side_effect=RuntimeError("state mismatch"))
    )
    observed = []

    async def observer(stage, details):
        observed.append((stage, details))

    runtime = BinanceExecutionRuntime(
        stream,
        poller,
        reconciler,
        reconciliation_observer=observer,
    )

    with pytest.raises(RuntimeError, match="state mismatch"):
        await runtime.reconcile_once("safety_periodic")

    assert [stage for stage, _ in observed] == ["started", "failed"]
    assert observed[0][1]["reason"] == "safety_periodic"
    assert observed[1][1]["reason"] == "safety_periodic"
    assert observed[0][1]["trace_id"] == observed[1][1]["trace_id"]
    assert observed[1][1]["error_type"] == "RuntimeError"
    assert observed[1][1]["error_message"] == "state mismatch"


@pytest.mark.asyncio
async def test_runtime_reconciliation_observer_failure_is_fail_closed():
    stream = Mock(start=AsyncMock(), stop=AsyncMock(), on_reconnect=None)
    poller = Mock(
        resolver=Mock(resolve_recovered_unknowns_once=AsyncMock(return_value={})),
        start=Mock(),
        stop=AsyncMock(),
    )
    reconciler = Mock(reconcile_once=AsyncMock())

    async def observer(_stage, _details):
        raise OSError("journal unavailable")

    runtime = BinanceExecutionRuntime(
        stream,
        poller,
        reconciler,
        reconciliation_observer=observer,
    )

    with pytest.raises(OSError, match="journal unavailable"):
        await runtime.start()

    reconciler.reconcile_once.assert_not_awaited()
    poller.start.assert_not_called()
    poller.stop.assert_awaited_once()
    stream.stop.assert_awaited_once()
    assert runtime.is_running is False


@pytest.mark.asyncio
async def test_runtime_reconciliation_observer_failure_does_not_replace_reconciliation_error():
    stream = Mock(start=AsyncMock(), stop=AsyncMock(), on_reconnect=None)
    poller = Mock(
        resolver=Mock(resolve_recovered_unknowns_once=AsyncMock(return_value={})),
        start=Mock(),
        stop=AsyncMock(),
    )
    reconciler = Mock(
        reconcile_once=AsyncMock(side_effect=RuntimeError("state mismatch"))
    )

    async def observer(stage, _details):
        if stage == "failed":
            raise OSError("journal unavailable")

    runtime = BinanceExecutionRuntime(
        stream,
        poller,
        reconciler,
        reconciliation_observer=observer,
    )

    with pytest.raises(RuntimeError, match="state mismatch") as raised:
        await runtime.reconcile_once("safety_periodic")

    notes = "\n".join(raised.value.__notes__ or ())
    assert "reconciliation failed observer raised OSError: journal unavailable" in notes


@pytest.mark.asyncio
async def test_runtime_start_failure_aborts_startup_recovery_before_stream_stop():
    calls = []
    stream = Mock(start=AsyncMock(), stop=AsyncMock(), on_reconnect=None)
    stream.stop.side_effect = lambda: calls.append("stream-stop")
    poller = Mock(
        resolver=Mock(resolve_recovered_unknowns_once=AsyncMock()),
        start=Mock(),
        stop=AsyncMock(side_effect=lambda: calls.append("poller-stop")),
    )
    reconciler = Mock(
        reconcile_once=AsyncMock(side_effect=RuntimeError("state mismatch"))
    )
    startup_failure = AsyncMock(side_effect=lambda: calls.append("abort-startup"))
    runtime = BinanceExecutionRuntime(
        stream,
        poller,
        reconciler,
        on_startup_failure=startup_failure,
    )

    with pytest.raises(RuntimeError, match="state mismatch"):
        await runtime.start()

    assert calls == ["abort-startup", "poller-stop", "stream-stop"]
    startup_failure.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_runtime_start_cleanup_errors_do_not_replace_startup_error():
    stream = Mock(start=AsyncMock(), stop=AsyncMock(), on_reconnect=None)
    stream.stop.side_effect = OSError("stream cleanup failed")
    poller = Mock(
        resolver=Mock(resolve_recovered_unknowns_once=AsyncMock()),
        start=Mock(),
        stop=AsyncMock(side_effect=ValueError("poller cleanup failed")),
    )
    reconciler = Mock(
        reconcile_once=AsyncMock(side_effect=RuntimeError("original startup error"))
    )
    startup_failure = AsyncMock(side_effect=LookupError("callback cleanup failed"))
    runtime = BinanceExecutionRuntime(
        stream,
        poller,
        reconciler,
        on_startup_failure=startup_failure,
    )

    with pytest.raises(RuntimeError, match="original startup error") as raised:
        await runtime.start()

    notes = "\n".join(raised.value.__notes__ or ())
    assert "startup failure callback" in notes
    assert "callback cleanup failed" in notes
    assert "unknown-order poller stop" in notes
    assert "poller cleanup failed" in notes
    assert "user data stream stop" in notes
    assert "stream cleanup failed" in notes
    assert runtime.is_running is False
