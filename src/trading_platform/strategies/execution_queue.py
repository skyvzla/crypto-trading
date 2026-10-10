"""Direction-neutral execution queue shared by dedicated strategy workers."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from enum import IntEnum
from typing import Awaitable, Callable, Literal

from trading_platform.shared.events import OrderIntent


class ExecutionPriority(IntEnum):
    REDUCE_ONLY_EXIT = 0
    CANCEL_ENTRY = 1
    ENTRY = 2


JobKind = Literal["exit", "cancel", "entry"]


@dataclass(order=True, slots=True)
class ExecutionJob:
    priority: int
    sequence: int
    kind: JobKind = field(compare=False)
    intent: OrderIntent | None = field(compare=False, default=None)
    event_time: int = field(compare=False, default=0)
    recovered: bool = field(compare=False, default=False)


def prepare_recovered_exit(job: ExecutionJob, account) -> bool | None:
    """Revalidate an undispatched exit after account reconciliation, without new I/O."""
    if not job.recovered:
        return True
    intent = job.intent
    assert intent is not None and intent.reduce_only
    if account.get_order(intent.client_order_id) is not None:
        return None  # The order WAL/reconciler already owns this submission.
    if account.has_unresolved_orders(intent.symbol) or account.has_pending_position_update(intent.symbol):
        return False
    position = account.get_position(intent.symbol)
    if position is None or position.quantity <= 0:
        return None
    if intent.side != ("SELL" if position.side == "LONG" else "BUY"):
        raise ValueError("recovered exit conflicts with the reconciled position side")
    job.intent = replace(intent, quantity=min(intent.quantity, position.quantity))
    return True


class ExecutionQueue:
    """One-account queue: exits and cancels cannot be blocked by entry backlog."""

    def __init__(self, *, max_pending_entries: int = 256) -> None:
        if max_pending_entries <= 0:
            raise ValueError("max_pending_entries must be positive")
        self.max_pending_entries = max_pending_entries
        self._queue: asyncio.PriorityQueue[ExecutionJob] = asyncio.PriorityQueue()
        self._next_sequence = 1
        self._pending_entries = 0
        self.store = None
        self._queued_ids: set[str] = set()

    async def put(self, kind: JobKind, *, intent: OrderIntent | None = None,
                  event_time: int = 0) -> ExecutionJob | None:
        if kind == "entry" and self._pending_entries >= self.max_pending_entries:
            raise asyncio.QueueFull
        if self.store is not None and intent is not None:
            if not await self.store.save(kind, intent, event_time):
                return None
            if intent.client_order_id in self._queued_ids:
                return None
        try:
            job = self.put_nowait(kind, intent=intent, event_time=event_time)
        except asyncio.QueueFull:
            if self.store is not None and intent is not None:
                await self.store.finish(intent.client_order_id, expired=True)
            raise
        if intent is not None:
            self._queued_ids.add(intent.client_order_id)
        return job

    async def restore(self, *, submitted: Callable[[str], bool] | None = None) -> None:
        """Restart entries need a fresh signal; acknowledged exit intents remain actionable."""
        if self.store is None:
            return
        for kind, event_time, intent in await self.store.pending():
            if submitted is not None and submitted(intent.client_order_id):
                await self.store.finish(intent.client_order_id)
            elif kind == "entry":
                await self.store.finish(intent.client_order_id, expired=True)
            elif intent.client_order_id not in self._queued_ids:
                job = self.put_nowait(kind, intent=intent, event_time=event_time)
                job.recovered = True
                self._queued_ids.add(intent.client_order_id)

    async def complete(self, job: ExecutionJob) -> None:
        if job.intent is not None:
            if self.store is not None:
                await self.store.finish(job.intent.client_order_id)
            self._queued_ids.discard(job.intent.client_order_id)

    def retry(self, job: ExecutionJob) -> None:
        job.sequence = self._next_sequence
        self._next_sequence += 1
        self._queue.put_nowait(job)

    @property
    def qsize(self) -> int:
        return self._queue.qsize()

    def _job(
        self,
        kind: JobKind,
        *,
        intent: OrderIntent | None,
        event_time: int,
    ) -> ExecutionJob:
        if event_time < 0:
            raise ValueError("event_time must be non-negative")
        priority = {
            "exit": ExecutionPriority.REDUCE_ONLY_EXIT,
            "cancel": ExecutionPriority.CANCEL_ENTRY,
            "entry": ExecutionPriority.ENTRY,
        }[kind]
        job = ExecutionJob(priority, self._next_sequence, kind, intent, event_time)
        self._next_sequence += 1
        return job

    def put_nowait(
        self,
        kind: JobKind,
        *,
        intent: OrderIntent | None = None,
        event_time: int = 0,
    ) -> ExecutionJob:
        if kind == "entry" and intent is None:
            raise ValueError("entry jobs require an intent")
        if kind == "exit" and (intent is None or not intent.reduce_only):
            raise ValueError("exit jobs require a reduce-only intent")
        if kind == "entry" and intent is not None and intent.reduce_only:
            raise ValueError("entry jobs cannot be reduce-only")
        if kind == "entry" and self._pending_entries >= self.max_pending_entries:
            raise asyncio.QueueFull
        job = self._job(kind, intent=intent, event_time=event_time)
        self._queue.put_nowait(job)
        if kind == "entry":
            self._pending_entries += 1
        return job

    async def get(self) -> ExecutionJob:
        job = await self._queue.get()
        if job.kind == "entry":
            self._pending_entries -= 1
        return job

    def task_done(self) -> None:
        self._queue.task_done()

    async def join(self) -> None:
        await self._queue.join()


class ExecutionWorker:
    """Run exchange I/O outside the signal loop and surface handler failures."""

    def __init__(
        self,
        queue: ExecutionQueue,
        handler: Callable[[ExecutionJob], Awaitable[bool | None]],
        *,
        task_name: str = "strategy-execution-worker",
    ) -> None:
        self.queue = queue
        self.handler = handler
        self.task_name = task_name
        self._task: asyncio.Task[None] | None = None

    def start(self) -> asyncio.Task[None]:
        if self._task is not None and not self._task.done():
            return self._task
        self._task = asyncio.create_task(self._run(), name=self.task_name)
        return self._task

    async def _run(self) -> None:
        while True:
            job = await self.queue.get()
            try:
                result = await self.handler(job)
                if result is False and job.kind == "exit":
                    await asyncio.sleep(0.5)
                    self.queue.retry(job)
                else:
                    await self.queue.complete(job)
            finally:
                self.queue.task_done()

    async def stop(self) -> None:
        task = self._task
        if task is None:
            return
        task.cancel()
        try:
            await asyncio.gather(task, return_exceptions=True)
        finally:
            if task.done() and self._task is task:
                self._task = None
