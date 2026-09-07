"""Binance User Stream 与未知订单恢复的运行生命周期。"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Mapping
from dataclasses import fields, is_dataclass
from typing import Any, Callable, Protocol
from uuid import uuid4

from trading_platform.shared.execution_recovery import SubmitUnknownPollingService

from .user_stream import UserDataStream


class StartupReconciler(Protocol):
    async def reconcile_once(self) -> object:
        ...


ReconciliationObserver = Callable[
    [str, dict[str, Any]], Awaitable[None] | None
]
StartupFailureCallback = Callable[[], Awaitable[None] | None]


def _reconciliation_result_details(result: object) -> dict[str, Any]:
    """Expose the stable result shape without coupling runtime to ledger types."""

    if is_dataclass(result) and not isinstance(result, type):
        result_fields = {
            field.name: getattr(result, field.name) for field in fields(result)
        }
    elif isinstance(result, Mapping):
        result_fields = dict(result)
    else:
        result_fields = {}
    return {
        "result_type": type(result).__name__,
        "result_fields": result_fields,
    }


class BinanceExecutionRuntime:
    """把 User Stream、启动对账和后台未知订单恢复组合为一个生命周期。"""

    def __init__(
        self,
        user_stream: UserDataStream,
        unknown_poller: SubmitUnknownPollingService,
        startup_reconciler: StartupReconciler | None = None,
        *,
        on_recovered: Callable[[], object] | None = None,
        reconciliation_observer: ReconciliationObserver | None = None,
        on_startup_failure: StartupFailureCallback | None = None,
    ):
        self.user_stream = user_stream
        self.unknown_poller = unknown_poller
        self.startup_reconciler = startup_reconciler
        self.on_recovered = on_recovered
        self.reconciliation_observer = reconciliation_observer
        self.on_startup_failure = on_startup_failure
        self._running = False
        self._previous_reconnect = user_stream.on_reconnect
        user_stream.on_reconnect = self._on_reconnect

    @property
    def is_running(self) -> bool:
        return self._running

    async def _observe_reconciliation(
        self, stage: str, details: dict[str, Any]
    ) -> None:
        observer = self.reconciliation_observer
        if observer is None:
            return
        result = observer(stage, details)
        if inspect.isawaitable(result):
            await result

    async def reconcile_once(self, reason: str = "manual") -> object | None:
        """Run one strict account reconciliation with an auditable lifecycle."""

        reconciler = self.startup_reconciler
        if reconciler is None:
            return None
        trace_id = uuid4().hex
        started = {"reason": reason, "trace_id": trace_id}
        await self._observe_reconciliation("started", started)
        try:
            result = await reconciler.reconcile_once()
        except BaseException as exc:
            failure = {
                "reason": reason,
                "trace_id": trace_id,
                "error_type": type(exc).__name__,
                "error_message": str(exc)[:500],
            }
            try:
                await self._observe_reconciliation("failed", failure)
            except BaseException as observer_exc:
                exc.add_note(
                    "reconciliation failed observer raised "
                    f"{type(observer_exc).__name__}: {observer_exc}"
                )
            raise
        completed = {
            "reason": reason,
            "trace_id": trace_id,
            **_reconciliation_result_details(result),
        }
        await self._observe_reconciliation("completed", completed)
        return result

    async def start(self) -> None:
        """建立事件流并完成一次启动对账，再启动后台有限重试。"""
        if self._running:
            return
        try:
            await self.user_stream.start()
            await self.unknown_poller.resolver.resolve_recovered_unknowns_once()
            if self.startup_reconciler is not None:
                await self.reconcile_once("startup")
            self.unknown_poller.start()
        except BaseException as startup_error:
            cleanup_steps = (
                ("startup failure callback", self.on_startup_failure),
                ("unknown-order poller stop", self.unknown_poller.stop),
                ("user data stream stop", self.user_stream.stop),
            )
            for label, cleanup in cleanup_steps:
                if cleanup is None:
                    continue
                try:
                    result = cleanup()
                    if inspect.isawaitable(result):
                        await result
                except BaseException as cleanup_error:
                    startup_error.add_note(
                        "startup cleanup failed in "
                        f"{label}: {type(cleanup_error).__name__}: {cleanup_error}"
                    )
            raise
        self._running = True

    async def stop(self) -> None:
        """停止接收新回报，并取消仍在运行的恢复任务。"""
        self._running = False
        try:
            await self.user_stream.stop()
        finally:
            await self.unknown_poller.stop()

    async def _on_reconnect(self) -> None:
        """重连后串行恢复 WAL，再重新启动未知订单轮询。"""
        await self.unknown_poller.stop()
        if self.startup_reconciler is not None:
            await self.reconcile_once("reconnect")
        self.unknown_poller.start()
        if self.on_recovered is not None:
            try:
                result = self.on_recovered()
                if inspect.isawaitable(result):
                    await result
            except BaseException:
                await self.unknown_poller.stop()
                raise
        callback = self._previous_reconnect
        if callback is None:
            return
        result = callback()
        if inspect.isawaitable(result):
            await result
