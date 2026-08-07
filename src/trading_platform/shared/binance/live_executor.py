"""带订单 WAL 的 Binance 实时提交适配器。"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import httpx

from trading_platform.shared.events import OrderIntent
from trading_platform.shared.execution_recovery import (
    OrderWAL,
    OrderWALRecord,
    Resolution,
    SubmitUnknownResolver,
)
from trading_platform.shared.risk import RiskGuard

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
        risk_guard: RiskGuard | None = None,
    ):
        self.rest_client = rest_client
        self.wal = wal
        self.account_id = account_id
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))
        self._resolver = SubmitUnknownResolver(wal, rest_client)
        self.risk_guard = risk_guard

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
                return self._record_unknown(
                    existing,
                    error="recovered_unresolved_intent",
                )
            if existing.status == "SUBMIT_UNKNOWN":
                self._block_unknown(existing)
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
            return self._record_unknown(
                intent_record,
                error=f"submit_timeout:{type(exc).__name__}",
            )

        try:
            return self.wal.record_exchange_status(
                intent_record,
                response,
                recorded_at=self._now_ms(),
            )
        except ValueError:
            return self._record_unknown(
                intent_record,
                error="unknown_submit_response_status",
            )

    async def resolve_submit_unknown(self, record: OrderWALRecord) -> Resolution:
        """对一个未知提交执行一次查单；未解析时保持未知。"""
        result = await self._resolver.resolve_once(record, recorded_at=self._now_ms())
        self._refresh_symbol_risk(record.symbol)
        return result

    async def resolve_recovered_unknowns_once(self) -> dict[str, Resolution]:
        """启动时对 WAL 中的未知提交各查询一次，不执行循环或重下单。"""
        results: dict[str, Resolution] = {}
        for client_order_id, record in self.wal.recover_latest().items():
            if record.record_type == "intent":
                record = self._record_unknown(
                    record,
                    error="recovered_unresolved_intent",
                )
            if record.status != "SUBMIT_UNKNOWN":
                continue
            self._block_unknown(record)
            results[client_order_id] = await self.resolve_submit_unknown(record)
        return results

    def handle_order_trade_update(
        self,
        order_data: dict[str, Any],
    ) -> OrderWALRecord | None:
        """将属于本执行器的 ``ORDER_TRADE_UPDATE.o`` 同步到 WAL。

        账户级 User Stream 还会包含人工订单或其他执行器订单；不在本 WAL、
        或 WAL 账户不匹配的 client id 明确忽略。属于本执行器的回报必须完整且
        与 WAL 身份一致，否则保持原事实并阻塞对应 symbol。
        """
        client_order_id = order_data.get("c")
        if not isinstance(client_order_id, str) or not client_order_id:
            raise ValueError("missing ORDER_TRADE_UPDATE client order id: c")

        record = self.wal.recover_latest().get(client_order_id)
        if record is None or record.account_id != self.account_id:
            return None

        try:
            symbol = order_data.get("s")
            if not isinstance(symbol, str) or not symbol:
                raise ValueError("missing ORDER_TRADE_UPDATE symbol: s")
            if symbol != record.symbol:
                raise ValueError(
                    f"ORDER_TRADE_UPDATE symbol mismatch: {symbol} != {record.symbol}"
                )

            status = order_data.get("X")
            if not isinstance(status, str) or not status:
                raise ValueError("missing ORDER_TRADE_UPDATE status: X")

            exchange_order_id = order_data.get("i")
            if exchange_order_id is None or exchange_order_id == "":
                raise ValueError("missing ORDER_TRADE_UPDATE exchange order id: i")
            if (
                record.exchange_order_id is not None
                and str(exchange_order_id) != record.exchange_order_id
            ):
                raise ValueError(
                    "ORDER_TRADE_UPDATE exchange order id mismatch: "
                    f"{exchange_order_id} != {record.exchange_order_id}"
                )

            updated = self.wal.record_exchange_status(
                record,
                {
                    "status": status,
                    "orderId": exchange_order_id,
                    "user_stream_order": dict(order_data),
                },
                recorded_at=self._now_ms(),
            )
        except ValueError:
            if self.risk_guard is not None:
                self.risk_guard.block_symbol(
                    record.symbol,
                    f"invalid ORDER_TRADE_UPDATE:{record.client_order_id}",
                )
            raise

        self._refresh_symbol_risk(record.symbol)
        return updated

    def _record_unknown(
        self,
        record: OrderWALRecord,
        *,
        error: str,
    ) -> OrderWALRecord:
        unknown = self.wal.record_submit_unknown(
            record,
            recorded_at=self._now_ms(),
            error=error,
        )
        self._block_unknown(unknown)
        return unknown

    def _block_unknown(self, record: OrderWALRecord) -> None:
        if self.risk_guard is not None:
            self.risk_guard.block_symbol(
                record.symbol,
                f"SUBMIT_UNKNOWN:{record.client_order_id}",
            )

    def _refresh_symbol_risk(self, symbol: str) -> None:
        if self.risk_guard is None:
            return
        remains_unknown = any(
            record.symbol == symbol
            and (
                record.record_type == "intent"
                or record.status == "SUBMIT_UNKNOWN"
            )
            for record in self.wal.recover_latest().values()
        )
        if remains_unknown:
            self.risk_guard.block_symbol(symbol, "SUBMIT_UNKNOWN pending")
        else:
            self.risk_guard.unblock_symbol(symbol)
