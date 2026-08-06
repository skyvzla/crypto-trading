"""带订单 WAL 的 Binance 实时提交适配器。"""

from __future__ import annotations

import time
from collections.abc import Callable

import httpx

from trading_platform.shared.events import OrderIntent
from trading_platform.shared.execution_recovery import (
    OrderWAL,
    OrderWALRecord,
    Resolution,
    SubmitUnknownResolver,
)

from .rest_client import BinanceRestClient


class BinanceOrderExecutor:
    """只负责可靠提交与查单，不决定重试、撤单或交易规则。"""

    def __init__(
        self,
        rest_client: BinanceRestClient,
        wal: OrderWAL,
        *,
        account_id: str,
        now_ms: Callable[[], int] | None = None,
    ):
        self.rest_client = rest_client
        self.wal = wal
        self.account_id = account_id
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))
        self._resolver = SubmitUnknownResolver(wal, rest_client)

    async def submit(
        self,
        intent: OrderIntent,
        *,
        reduce_only: bool = False,
    ) -> OrderWALRecord:
        """提交一次订单；相同 ``client_order_id`` 永不自动重复提交。"""
        existing = self.wal.recover_latest().get(intent.client_order_id)
        if existing is not None:
            if existing.record_type == "intent":
                return self.wal.record_submit_unknown(
                    existing,
                    recorded_at=self._now_ms(),
                    error="recovered_unresolved_intent",
                )
            return existing

        intent_record = self.wal.record_intent(
            intent,
            account_id=self.account_id,
            recorded_at=self._now_ms(),
        )
        try:
            response = await self.rest_client.post_order(
                symbol=intent.symbol,
                side=intent.side,
                order_type=intent.order_type,
                quantity=intent.quantity,
                price=intent.price if intent.order_type == "LIMIT" else None,
                new_client_order_id=intent.client_order_id,
                reduce_only=reduce_only,
            )
        except (httpx.TimeoutException, RuntimeError) as exc:
            return self.wal.record_submit_unknown(
                intent_record,
                recorded_at=self._now_ms(),
                error=f"submit_timeout:{type(exc).__name__}",
            )

        try:
            return self.wal.record_exchange_status(
                intent_record,
                response,
                recorded_at=self._now_ms(),
            )
        except ValueError:
            return self.wal.record_submit_unknown(
                intent_record,
                recorded_at=self._now_ms(),
                error="unknown_submit_response_status",
            )

    async def resolve_submit_unknown(self, record: OrderWALRecord) -> Resolution:
        """对一个未知提交执行一次查单；未解析时保持未知。"""
        return await self._resolver.resolve_once(record, recorded_at=self._now_ms())
