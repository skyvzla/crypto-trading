import asyncio

import pytest

from trading_platform.shared.events import OrderIntent
from trading_platform.strategies.spike.execution_queue import (
    ExecutionQueue,
    ExecutionWorker,
)


def entry() -> OrderIntent:
    return OrderIntent(
        symbol="BTCUSDT",
        side="SELL",
        price=100,
        quantity=1,
        client_order_id="entry",
        strategy_id="spike_short",
    )


def exit_intent() -> OrderIntent:
    return OrderIntent(
        symbol="BTCUSDT",
        side="BUY",
        price=100,
        quantity=1,
        client_order_id="exit",
        reduce_only=True,
        strategy_id="spike_short",
    )


@pytest.mark.asyncio
async def test_execution_priority_is_exit_then_cancel_then_entry_and_fifo_within_kind():
    queue = ExecutionQueue()
    queue.put_nowait("entry", intent=entry(), event_time=1)
    queue.put_nowait("cancel", event_time=2)
    queue.put_nowait("exit", intent=exit_intent(), event_time=3)
    queue.put_nowait("entry", intent=entry(), event_time=4)

    jobs = [await queue.get() for _ in range(4)]
    assert [(job.kind, job.sequence) for job in jobs] == [
        ("exit", 3),
        ("cancel", 2),
        ("entry", 1),
        ("entry", 4),
    ]
    for _ in jobs:
        queue.task_done()


@pytest.mark.asyncio
async def test_worker_handles_network_work_outside_event_enqueue_path():
    queue = ExecutionQueue()
    handled = []
    gate = asyncio.Event()

    async def handle(job):
        handled.append(job.kind)
        gate.set()

    worker = ExecutionWorker(queue, handle)
    worker.start()
    queue.put_nowait("entry", intent=entry())
    await asyncio.wait_for(gate.wait(), timeout=1)
    await queue.join()
    assert handled == ["entry"]
    await worker.stop()


@pytest.mark.asyncio
async def test_invalid_priority_payload_is_rejected():
    queue = ExecutionQueue()
    with pytest.raises(ValueError, match="reduce-only"):
        queue.put_nowait("entry", intent=exit_intent())
    with pytest.raises(ValueError, match="reduce-only"):
        queue.put_nowait("exit", intent=entry())


@pytest.mark.asyncio
async def test_entry_backlog_is_bounded_without_blocking_exit_or_cancel():
    queue = ExecutionQueue(max_pending_entries=1)
    queue.put_nowait("entry", intent=entry())

    with pytest.raises(asyncio.QueueFull):
        queue.put_nowait("entry", intent=entry())

    queue.put_nowait("exit", intent=exit_intent())
    queue.put_nowait("cancel")
    assert [
        (await queue.get()).kind,
        (await queue.get()).kind,
        (await queue.get()).kind,
    ] == ["exit", "cancel", "entry"]


@pytest.mark.asyncio
async def test_worker_failure_is_visible_to_owner_and_stop_is_idempotent():
    queue = ExecutionQueue()

    async def fail(_job):
        raise RuntimeError("submit failed")

    worker = ExecutionWorker(queue, fail)
    task = worker.start()
    queue.put_nowait("entry", intent=entry())

    with pytest.raises(RuntimeError, match="submit failed"):
        await task
    await worker.stop()
    await worker.stop()


@pytest.mark.asyncio
async def test_worker_stop_cancels_in_flight_network_handler():
    queue = ExecutionQueue()
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def wait_forever(_job):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    worker = ExecutionWorker(queue, wait_forever)
    worker.start()
    queue.put_nowait("entry", intent=entry())
    await asyncio.wait_for(started.wait(), timeout=1)

    await worker.stop()

    assert cancelled.is_set()
