"""Binance User Stream 与未知订单恢复的运行生命周期。"""

from __future__ import annotations

import inspect
from typing import Protocol

from trading_platform.shared.execution_recovery import SubmitUnknownPollingService

from .user_stream import UserDataStream


class StartupReconciler(Protocol):
    async def reconcile_once(self) -> object:
        ...


class BinanceExecutionRuntime:
    """把 User Stream、启动对账和后台未知订单恢复组合为一个生命周期。"""

    def __init__(
        self,
        user_stream: UserDataStream,
        unknown_poller: SubmitUnknownPollingService,
        startup_reconciler: StartupReconciler | None = None,
    ):
        self.user_stream = user_stream
        self.unknown_poller = unknown_poller
        self.startup_reconciler = startup_reconciler
        self._running = False
        self._previous_reconnect = user_stream.on_reconnect
        user_stream.on_reconnect = self._on_reconnect

    @property
    def is_running(self) -> bool:
        return self._running

    async def start(self) -> None:
        """建立事件流并完成一次启动对账，再启动后台有限重试。"""
        if self._running:
            return
        await self.user_stream.start()
        try:
            await self.unknown_poller.resolver.resolve_recovered_unknowns_once()
            if self.startup_reconciler is not None:
                await self.startup_reconciler.reconcile_once()
            self.unknown_poller.start()
        except BaseException:
            await self.unknown_poller.stop()
            await self.user_stream.stop()
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
            await self.startup_reconciler.reconcile_once()
        self.unknown_poller.start()
        callback = self._previous_reconnect
        if callback is None:
            return
        result = callback()
        if inspect.isawaitable(result):
            await result
