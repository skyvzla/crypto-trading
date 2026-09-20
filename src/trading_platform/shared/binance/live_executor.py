"""带订单 WAL 的 Binance 实时提交适配器。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from decimal import Decimal
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

from .rest_client import BinanceAPIException, BinanceRestClient
from .symbol_rules import BinanceSymbolRuleBook, SymbolRuleViolation


# Binance returned a structured business rejection, so these codes prove the
# order was not accepted. Transport/timeout and generic server errors remain
# SUBMIT_UNKNOWN and must be reconciled by client order id.
_DEFINITE_ORDER_REJECTION_CODES = {
    *range(-1136, -1099),
    -1013,
    -2010,
    -2018,
    -2019,
    -2020,
    -2021,
    -2022,
    -2024,
    -2025,
    -2026,
    -2027,
    -2028,
}


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
        symbol_rules: BinanceSymbolRuleBook | None = None,
        can_open_symbol: Callable[[str], bool] | None = None,
    ):
        self.rest_client = rest_client
        self.wal = wal
        self.account_id = account_id
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))
        self._resolver = SubmitUnknownResolver(wal, rest_client)
        self.risk_guard = risk_guard
        self.symbol_rules = symbol_rules
        self.can_open_symbol = can_open_symbol

    async def submit(
        self,
        intent: OrderIntent,
        *,
        reference_price: Decimal | None = None,
        leverage: int = 1,
    ) -> OrderWALRecord:
        """提交一次订单；相同 ``client_order_id`` 永不自动重复提交。"""
        if not intent.campaign_id:
            raise ValueError("campaign_id is required before order submission")
        if (
            not intent.reduce_only
            and self.can_open_symbol is not None
            and not self.can_open_symbol(intent.symbol)
        ):
            raise SymbolRuleViolation(
                f"new entries are blocked for {intent.symbol} by exchange metadata"
            )
        if self.symbol_rules is not None:
            intent = self.symbol_rules.get(intent.symbol).normalize_intent(
                intent,
                reference_price=reference_price,
            )
        existing = self.wal.recover_latest().get(intent.client_order_id)
        if existing is not None:
            if not self._same_intent(existing, intent):
                self._block_unknown(existing)
                raise ValueError(
                    f"client_order_id reused with different intent: "
                    f"{intent.client_order_id}"
                )
            if not intent.reduce_only:
                self._restore_risk_reservation(existing)
            if existing.record_type == "intent":
                return self._record_unknown(
                    existing,
                    error="recovered_unresolved_intent",
                )
            if existing.status == "SUBMIT_UNKNOWN":
                self._block_unknown(existing)
            return existing
        risk_value: Decimal | None = None
        risk_price: Decimal | None = None
        if not intent.reduce_only and self.risk_guard is not None:
            notional_price = (
                intent.price if intent.order_type == "LIMIT" else reference_price
            )
            if notional_price is None:
                raise ValueError("market entry requires reference_price")
            risk_price = notional_price
            risk_value = intent.quantity * notional_price
            allowed, reason = self.risk_guard.reserve_open(
                intent.symbol,
                risk_value,
                intent.client_order_id,
                leverage=leverage,
            )
            if not allowed:
                raise PermissionError(f"order rejected by risk guard: {reason}")

        try:
            intent_record = self.wal.record_intent(
                intent,
                account_id=self.account_id,
                recorded_at=self._now_ms(),
                risk_value_usdt=risk_value,
                risk_price_usdt=risk_price,
            )
        except BaseException:
            if not intent.reduce_only and self.risk_guard is not None:
                self.risk_guard.release_open(intent.client_order_id)
            raise
        try:
            response = await self.rest_client.post_order(
                symbol=intent.symbol,
                side=intent.side,
                order_type=intent.order_type,
                quantity=intent.quantity,
                price=intent.price if intent.order_type == "LIMIT" else None,
                new_client_order_id=intent.client_order_id,
                reduce_only=intent.reduce_only,
            )
        except asyncio.CancelledError:
            latest = self.wal.recover_latest().get(intent.client_order_id)
            if latest is None or latest.record_type == "intent":
                self._record_unknown(
                    latest or intent_record,
                    error="submit_cancelled",
                )
            raise
        except BinanceAPIException as exc:
            if exc.code in _DEFINITE_ORDER_REJECTION_CODES:
                rejected = self.wal.record_exchange_status(
                    intent_record,
                    {
                        "status": "REJECTED",
                        "code": exc.code,
                        "msg": exc.message,
                    },
                    recorded_at=self._now_ms(),
                )
                self._sync_risk_record(rejected)
                self._refresh_symbol_risk(intent.symbol)
                return rejected
            return self._record_unknown(
                intent_record,
                error=f"submit_api_ambiguous:{exc.code}",
            )
        except (httpx.TimeoutException, RuntimeError) as exc:
            return self._record_unknown(
                intent_record,
                error=f"submit_timeout:{type(exc).__name__}",
            )

        # User Stream can report NEW/PARTIALLY_FILLED/FILLED before the REST
        # request task resumes.  Its later WAL row is the newer exchange fact;
        # never append the older submit response over it.
        latest = self.wal.recover_latest().get(intent.client_order_id)
        if latest is not None and latest.record_type != "intent":
            response_order_id = response.get("orderId")
            if (
                response_order_id is not None
                and latest.exchange_order_id is not None
                and str(response_order_id) != latest.exchange_order_id
            ):
                self._block_unknown(latest)
                raise ValueError("REST and User Stream order ids differ")
            return latest

        try:
            result = self.wal.record_exchange_status(
                intent_record,
                response,
                recorded_at=self._now_ms(),
            )
            self._sync_risk_record(result)
            return result
        except ValueError:
            return self._record_unknown(
                intent_record,
                error="unknown_submit_response_status",
            )

    async def resolve_submit_unknown(self, record: OrderWALRecord) -> Resolution:
        """对一个未知提交执行一次查单；未解析时保持未知。"""
        result = await self._resolver.resolve_once(record, recorded_at=self._now_ms())
        latest = self.wal.recover_latest().get(record.client_order_id)
        if latest is not None:
            self._sync_risk_record(latest)
            self._refresh_symbol_risk(latest.symbol)
        else:
            self._refresh_symbol_risk(record.symbol)
        return result

    async def resolve_recovered_unknowns_once(self) -> dict[str, Resolution]:
        """启动时对 WAL 中的未知提交各查询一次，不执行循环或重下单。"""
        results: dict[str, Resolution] = {}
        for client_order_id, record in self.wal.recover_latest().items():
            if not bool(record.payload.get("reduce_only", False)):
                self._restore_risk_reservation(record)
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
            self._sync_risk_record(updated, order_data=order_data)
        except ValueError:
            if self.risk_guard is not None:
                self.risk_guard.block_symbol(
                    record.symbol,
                    f"invalid ORDER_TRADE_UPDATE:{record.client_order_id}",
                )
            raise

        self._refresh_symbol_risk(record.symbol)
        return updated

    def reconcile_order_response(
        self,
        response: dict[str, Any],
    ) -> OrderWALRecord:
        """将 REST 查单事实严格合并到 WAL，供启动恢复使用。"""
        client_order_id = response.get("clientOrderId")
        if not isinstance(client_order_id, str) or not client_order_id:
            raise ValueError("missing query order clientOrderId")
        record = self.wal.recover_latest().get(client_order_id)
        if record is None or record.account_id != self.account_id:
            raise ValueError(f"query order is not owned by WAL: {client_order_id}")
        try:
            if response.get("symbol") != record.symbol:
                raise ValueError("query order symbol mismatch")
            if response.get("side") != record.side:
                raise ValueError("query order side mismatch")
            if response.get("type") != record.order_type:
                raise ValueError("query order type mismatch")
            if Decimal(str(response.get("origQty"))) != Decimal(record.quantity):
                raise ValueError("query order quantity mismatch")
            exchange_order_id = response.get("orderId")
            if exchange_order_id is None or exchange_order_id == "":
                raise ValueError("missing query order orderId")
            if (
                record.exchange_order_id is not None
                and str(exchange_order_id) != record.exchange_order_id
            ):
                raise ValueError("query order exchange id mismatch")
            updated = self.wal.record_exchange_status(
                record,
                response,
                recorded_at=self._now_ms(),
            )
            self._sync_risk_record(updated)
        except (ArithmeticError, TypeError, ValueError):
            if self.risk_guard is not None:
                self.risk_guard.block_symbol(
                    record.symbol,
                    f"invalid query order response:{record.client_order_id}",
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
        self._restore_risk_reservation(record)
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

    def _restore_risk_reservation(
        self,
        record: OrderWALRecord,
        *,
        response: dict[str, Any] | None = None,
        include_fills: bool = True,
    ) -> None:
        if self.risk_guard is None or bool(record.payload.get("reduce_only", False)):
            return
        risk_value = self._risk_value(record)
        exchange_response = response
        if exchange_response is None:
            exchange_response = record.payload.get("exchange_response") or {}
        filled_value = (
            self._filled_value(record, exchange_response) if include_fills else None
        )
        fill_time = self._fill_time(exchange_response)
        status = record.status
        if filled_value is not None:
            has_fills: bool | None = filled_value > 0
        elif status == "REJECTED":
            has_fills = False
        elif status in {"FILLED", "PARTIALLY_FILLED"}:
            has_fills = True
        else:
            # A cancelled/expired response without executedQty is not proof that
            # the order had no fills. Keep the reservation until an account fact
            # or a later response makes that explicit.
            has_fills = None
        self.risk_guard.restore_open_reservation(
            client_order_id=record.client_order_id,
            symbol=record.symbol,
            value_usdt=risk_value,
            status=status,
            has_fills=has_fills,
            filled_value_usdt=filled_value,
            fill_time_ms=fill_time,
        )

    def _sync_risk_record(
        self,
        record: OrderWALRecord,
        *,
        order_data: dict[str, Any] | None = None,
    ) -> None:
        if self.risk_guard is None or bool(record.payload.get("reduce_only", False)):
            return
        if order_data is not None and order_data.get("x") == "TRADE":
            # Incremental quantity is recorded by BinanceStrategyAccount after
            # trade-id de-duplication. Do not feed the cumulative ``z`` field
            # into the reservation here as well.
            self._restore_risk_reservation(
                record,
                response=order_data,
                include_fills=False,
            )
            self.risk_guard.update_order_status(
                record.client_order_id,
                record.status or "SUBMIT_UNKNOWN",
                has_fills=True,
                fill_time_ms=self._fill_time(order_data),
            )
            return
        self._restore_risk_reservation(record)

    @staticmethod
    def _risk_value(record: OrderWALRecord) -> Decimal | None:
        raw = record.payload.get("risk_value_usdt")
        if raw is not None:
            try:
                value = Decimal(str(raw))
                if value > 0 and value.is_finite():
                    return value
            except (ArithmeticError, ValueError):
                pass
        # LIMIT 旧 WAL 至少能从原始价格重建。MARKET 的旧 WAL 通常以零价格
        # 存储，缺少当时的 reference price 时必须按最大额度保守占用。
        if record.order_type != "LIMIT":
            return None
        try:
            value = Decimal(record.quantity) * Decimal(record.price)
            return value if value > 0 and value.is_finite() else None
        except (ArithmeticError, ValueError):
            return None

    @classmethod
    def _filled_value(
        cls, record: OrderWALRecord, response: dict[str, Any]
    ) -> Decimal | None:
        nested = response.get("user_stream_order")
        if isinstance(nested, dict):
            response = nested

        raw_quote = response.get("Z")
        if raw_quote is None:
            raw_quote = response.get("quoteQty")
        if raw_quote is not None:
            try:
                value = Decimal(str(raw_quote))
                if value >= 0 and value.is_finite() and value > 0:
                    return value
            except (ArithmeticError, ValueError):
                pass

        raw_qty = response.get("z")
        if raw_qty is None:
            raw_qty = response.get("executedQty")
        if raw_qty is None:
            return None
        try:
            qty = Decimal(str(raw_qty))
            if not qty.is_finite() or qty < 0:
                return None
            if qty == 0:
                return Decimal("0")
            price_raw = record.payload.get("risk_price_usdt")
            if price_raw is None and record.order_type == "LIMIT":
                price_raw = record.price
            if price_raw is None:
                return None
            value = qty * Decimal(str(price_raw))
            return value if value > 0 and value.is_finite() else None
        except (ArithmeticError, ValueError):
            return None

    @staticmethod
    def _fill_time(response: dict[str, Any]) -> int | None:
        nested = response.get("user_stream_order")
        if isinstance(nested, dict):
            response = nested
        for key in ("T", "updateTime", "E", "time"):
            raw = response.get(key)
            if raw is None:
                continue
            try:
                return int(raw)
            except (TypeError, ValueError):
                continue
        return None

    @staticmethod
    def _same_intent(record: OrderWALRecord, intent: OrderIntent) -> bool:
        return (
            record.symbol == intent.symbol
            and record.side == intent.side
            and record.order_type == intent.order_type
            and Decimal(record.quantity) == intent.quantity
            and Decimal(record.price) == intent.price
            and bool(record.payload.get("reduce_only", False)) == intent.reduce_only
            and record.payload.get("campaign_id") == intent.campaign_id
        )
