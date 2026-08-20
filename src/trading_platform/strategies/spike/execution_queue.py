"""Spike 执行意图的有界优先级队列。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
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


class ExecutionQueue:
    """单一账户执行队列；限制积压入场，不限制退出与撤单。"""

    def __init__(self, *, max_pending_entries: int = 256) -> None:
        if max_pending_entries <= 0:
            raise ValueError("max_pending_entries must be positive")
        self.max_pending_entries = max_pending_entries
        self._queue: asyncio.PriorityQueue[ExecutionJob] = asyncio.PriorityQueue()
        self._next_sequence = 1
        self._pending_entries = 0

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
    """把网络执行放在独立任务中，异常交给调用方的 fail-closed handler。"""

    def __init__(
        self,
        queue: ExecutionQueue,
        handler: Callable[[ExecutionJob], Awaitable[None]],
    ) -> None:
        self.queue = queue
        self.handler = handler
        self._task: asyncio.Task[None] | None = None

    def start(self) -> asyncio.Task[None]:
        if self._task is not None and not self._task.done():
            return self._task
        self._task = asyncio.create_task(self._run(), name="spike-execution-worker")
        return self._task

    async def _run(self) -> None:
        while True:
            job = await self.queue.get()
            try:
                await self.handler(job)
            finally:
                self.queue.task_done()

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
