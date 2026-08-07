"""subcategory 策略准入与关闭撤单编排。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol

from trading_platform.shared.execution import StrategyAccount


class AdmissionSource(Protocol):
    async def is_subcategory_enabled(self, subcategory: str) -> bool:
        ...


class EntryGate(Protocol):
    def set_entry_enabled(self, enabled: bool) -> None:
        ...


@dataclass(frozen=True)
class AdmissionRefreshResult:
    enabled: bool
    source_healthy: bool
    account_healthy: bool = True
    cancelled_order_ids: tuple[str, ...] = ()
    failed_cancel_order_ids: tuple[str, ...] = ()
    unresolved_unknown_order_ids: tuple[str, ...] = ()


class SubcategoryAdmissionService:
    """由可交易池扫描驱动准入刷新；关闭或数据源异常时 fail-closed。

    该服务不创建自己的定时任务。调用方应在每次可交易池扫描完成后调用
    :meth:`on_universe_scan`，使 subcategory 与扫描周期天然一致。
    """

    def __init__(
        self,
        *,
        source: AdmissionSource,
        gate: EntryGate,
        account: StrategyAccount,
        subcategory: str,
        strategy_id: str,
        entry_trigger_reasons: set[str],
    ):
        if not subcategory:
            raise ValueError("subcategory is required")
        if not strategy_id:
            raise ValueError("strategy_id is required")
        if not entry_trigger_reasons:
            raise ValueError("entry_trigger_reasons must not be empty")
        self.source = source
        self.gate = gate
        self.account = account
        self.subcategory = subcategory
        self.strategy_id = strategy_id
        self.entry_trigger_reasons = frozenset(entry_trigger_reasons)
        self.last_result: AdmissionRefreshResult | None = None
        self.last_error: Exception | None = None

    async def on_universe_scan(self) -> AdmissionRefreshResult:
        try:
            enabled = await self.source.is_subcategory_enabled(self.subcategory)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            enabled = False
            source_healthy = False
            self.last_error = exc
        else:
            source_healthy = True
            self.last_error = None

        self.gate.set_entry_enabled(enabled)
        cancelled: list[str] = []
        failed: list[str] = []
        unknown: list[str] = []
        account_healthy = True
        if not enabled:
            try:
                orders = tuple(self.account.iter_orders())
            except Exception as exc:
                orders = ()
                account_healthy = False
                self.last_error = exc
            for order in orders:
                if (
                    order.strategy_id != self.strategy_id
                    or order.trigger_reason not in self.entry_trigger_reasons
                ):
                    continue
                if order.status == "SUBMIT_UNKNOWN":
                    unknown.append(order.order_id)
                elif order.status in {"NEW", "PARTIALLY_FILLED"}:
                    try:
                        accepted = self.account.cancel_order(order.order_id)
                    except Exception as exc:
                        accepted = False
                        account_healthy = False
                        self.last_error = exc
                    target = cancelled if accepted else failed
                    target.append(order.order_id)

        result = AdmissionRefreshResult(
            enabled=enabled,
            source_healthy=source_healthy,
            account_healthy=account_healthy,
            cancelled_order_ids=tuple(cancelled),
            failed_cancel_order_ids=tuple(failed),
            unresolved_unknown_order_ids=tuple(unknown),
        )
        self.last_result = result
        return result

    async def refresh_once(self) -> AdmissionRefreshResult:
        """兼容旧调用方；新代码应使用 ``on_universe_scan``。"""
        return await self.on_universe_scan()
