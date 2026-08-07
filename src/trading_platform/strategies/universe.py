"""可交易池扫描节拍与准入刷新编排。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable


UNIVERSE_SCAN_INTERVAL_SECONDS = 5 * 60


class UniverseScanLoop:
    """每五分钟执行一次扫描，并在同一节拍刷新 subcategory 准入。"""

    def __init__(
        self,
        scan_once: Callable[[], Awaitable[object]],
        admission_services: Iterable[object] = (),
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.scan_once = scan_once
        self.admission_services = tuple(admission_services)
        self._sleep = sleep

    async def run_once(self) -> object:
        result = await self.scan_once()
        for service in self.admission_services:
            await service.on_universe_scan()
        return result

    async def run(self) -> None:
        while True:
            await self.run_once()
            await self._sleep(UNIVERSE_SCAN_INTERVAL_SECONDS)
