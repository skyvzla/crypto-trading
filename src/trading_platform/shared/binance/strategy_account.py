"""Binance Futures 实时账户的进程内策略视图。"""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from trading_platform.shared.events import Fill, Order, Position
from trading_platform.shared.execution_recovery import OrderWAL, OrderWALRecord
from trading_platform.shared.risk import RiskGuard

from .rest_client import BinanceRestClient


_TERMINAL = {"FILLED", "CANCELLED", "EXPIRED"}


class BinanceStrategyAccount:
    """把异步交易所事实投影为策略核心需要的同步只读接口。

    同步 ``cancel_order`` 只登记撤单请求。外围事件循环必须调用
    ``flush_cancellations``，这样策略核心无需感知 asyncio。
    """

    def __init__(
        self,
        rest_client: BinanceRestClient,
        wal: OrderWAL,
        *,
        account_id: str,
        strategy_id: str,
        risk_guard: RiskGuard,
        now_ms: Callable[[], int] | None = None,
    ):
        self.rest_client = rest_client
        self.wal = wal
        self.account_id = account_id
        self.strategy_id = strategy_id
        self.risk_guard = risk_guard
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))
        self._positions: dict[str, Position] = {}
        self._position_update_ms: dict[str, int] = {}
        self._pending_position_update_ms: dict[str, int] = {}
        self._commissions: dict[str, Decimal] = {}
        self._processed_trade_ids: set[tuple[str, str]] = set()
        self._cancel_requests: set[str] = set()
        self._cancel_lock = asyncio.Lock()
        self._lock = asyncio.Lock()

    def get_order(self, order_id: str) -> Order | None:
        for order in self.iter_orders():
            if order.order_id == order_id:
                return order
        return None

    def iter_orders(self) -> tuple[Order, ...]:
        return tuple(
            self._to_order(record)
            for record in self.wal.recover_latest().values()
            if record.account_id == self.account_id
        )

    def has_open_position(self, symbol: str) -> bool:
        position = self._positions.get(symbol)
        return position is not None and position.quantity > 0

    def get_position(self, symbol: str) -> Position | None:
        position = self._positions.get(symbol)
        return position if position is not None and position.quantity > 0 else None

    def restore_trade_state(
        self,
        symbol: str,
        commission: Decimal,
        trade_ids: set[str],
    ) -> None:
        """用持久化成交事实恢复手续费和进程内成交幂等状态。"""
        if commission < 0:
            raise ValueError("commission must be non-negative")
        position = self.get_position(symbol)
        if position is None:
            raise RuntimeError(f"cannot restore commission without position: {symbol}")
        self._commissions[symbol] = commission
        self._processed_trade_ids.update((symbol, trade_id) for trade_id in trade_ids)
        position.total_commission = commission

    def cancel_order(self, order_id: str) -> bool:
        order = self.get_order(order_id)
        if order is None or order.status not in {"NEW", "PARTIALLY_FILLED"}:
            return False
        self._cancel_requests.add(order.client_order_id)
        return True

    async def flush_cancellations(self) -> tuple[str, ...]:
        """提交已登记撤单；失败时保留请求并阻塞该 symbol。"""
        cancelled: list[str] = []
        async with self._cancel_lock:
            for client_order_id in tuple(self._cancel_requests):
                record = self.wal.recover_latest().get(client_order_id)
                if record is None or record.status in _TERMINAL:
                    self._cancel_requests.discard(client_order_id)
                    continue
                try:
                    response = await self.rest_client.cancel_order(
                        record.symbol,
                        orig_client_order_id=client_order_id,
                    )
                    latest = self.wal.recover_latest().get(client_order_id)
                    if latest is None:
                        raise RuntimeError("cancelled order disappeared from WAL")
                    if latest.status not in _TERMINAL:
                        self.wal.record_exchange_status(
                            latest, response, recorded_at=self._now_ms()
                        )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    latest = self.wal.recover_latest().get(client_order_id)
                    if latest is not None and latest.status in _TERMINAL:
                        self._cancel_requests.discard(client_order_id)
                        cancelled.append(client_order_id)
                        continue
                    try:
                        exchange_order = await self.rest_client.query_order(
                            record.symbol,
                            orig_client_order_id=client_order_id,
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        exchange_order = None
                    if (
                        latest is not None
                        and exchange_order is not None
                        and str(exchange_order.get("status") or "")
                        in {"FILLED", "CANCELED", "EXPIRED"}
                    ):
                        self.wal.record_exchange_status(
                            latest, exchange_order, recorded_at=self._now_ms()
                        )
                        self._cancel_requests.discard(client_order_id)
                        cancelled.append(client_order_id)
                        continue
                    self.risk_guard.block_symbol(
                        record.symbol,
                        f"cancel unresolved:{client_order_id}:{type(exc).__name__}",
                    )
                    continue
                self._cancel_requests.discard(client_order_id)
                cancelled.append(client_order_id)
        return tuple(cancelled)

    @property
    def has_pending_cancellations(self) -> bool:
        return bool(self._cancel_requests)

    def has_pending_position_update(self, symbol: str) -> bool:
        """Return whether a fill still awaits a matching account position fact."""
        return symbol in self._pending_position_update_ms

    async def refresh_positions(self) -> None:
        """以 REST 快照初始化/修复本地仓位；较新的流事件不会被旧快照覆盖。"""
        snapshots = await self.rest_client.get_position_risk()
        if not isinstance(snapshots, list):
            raise RuntimeError("invalid Binance position snapshot")
        async with self._lock:
            seen: set[str] = set()
            for raw in snapshots:
                symbol = str(raw.get("symbol") or "")
                if not symbol:
                    raise RuntimeError("position snapshot missing symbol")
                update_ms = int(raw.get("updateTime") or 0)
                if update_ms < self._position_update_ms.get(symbol, -1):
                    continue
                amount = self._decimal(raw, "positionAmt")
                position_side = str(raw.get("positionSide") or "BOTH")
                if position_side != "BOTH":
                    raise RuntimeError("Binance position snapshot is not one-way")
                self._apply_position(
                    symbol=symbol,
                    amount=amount,
                    entry_price=self._decimal(raw, "entryPrice"),
                    unrealized_pnl=self._decimal(raw, "unRealizedProfit", default="0"),
                    update_ms=update_ms,
                    position_side=position_side,
                    confirms_stream_fill=False,
                )
                seen.add(symbol)

    async def handle_account_update(self, event: dict[str, Any]) -> None:
        if event.get("e") != "ACCOUNT_UPDATE":
            raise ValueError("expected ACCOUNT_UPDATE")
        update_ms = int(event["T"])
        positions = event.get("a", {}).get("P")
        if not isinstance(positions, list):
            raise ValueError("ACCOUNT_UPDATE missing a.P")
        async with self._lock:
            for raw in positions:
                symbol = str(raw.get("s") or "")
                if not symbol:
                    raise ValueError("ACCOUNT_UPDATE position missing symbol")
                if update_ms < self._position_update_ms.get(symbol, -1):
                    continue
                position_side = str(raw.get("ps") or "BOTH")
                if position_side != "BOTH":
                    raise RuntimeError("Binance account update is not one-way")
                self._apply_position(
                    symbol=symbol,
                    amount=self._decimal(raw, "pa"),
                    entry_price=self._decimal(raw, "ep"),
                    unrealized_pnl=self._decimal(raw, "up"),
                    update_ms=update_ms,
                    position_side=position_side,
                    confirms_stream_fill=True,
                )

    def handle_execution_report(self, order_data: dict[str, Any]) -> Fill | None:
        """从成交回报生成策略 Fill；WAL 更新由可靠提交器先行完成。"""
        if order_data.get("x") != "TRADE":
            return None
        quantity = self._decimal(order_data, "l", default="0")
        if quantity <= 0:
            return None
        client_order_id = str(order_data.get("c") or "")
        record = self.wal.recover_latest().get(client_order_id)
        if record is None or record.account_id != self.account_id:
            return None
        raw_trade_id = order_data.get("t")
        if raw_trade_id in (None, -1, "-1"):
            return None
        trade_id = str(raw_trade_id)
        fill_time = int(order_data.get("T") or self._now_ms())
        commission = self._decimal(order_data, "n", default="0")
        fill = Fill(
            fill_id=trade_id,
            order_id=record.exchange_order_id or record.client_order_id,
            symbol=record.symbol,
            side=record.side,
            price=self._decimal(order_data, "L"),
            quantity=quantity,
            commission=commission,
            commission_asset=str(order_data.get("N") or ""),
            fill_time=fill_time,
            is_maker=bool(order_data.get("m", False)),
        )
        trade_key = (record.symbol, trade_id)
        if trade_key in self._processed_trade_ids:
            return None
        self._processed_trade_ids.add(trade_key)
        if fill_time > self._position_update_ms.get(record.symbol, -1):
            self._pending_position_update_ms[record.symbol] = max(
                fill_time,
                self._pending_position_update_ms.get(record.symbol, -1),
            )
        self._commissions[record.symbol] = (
            self._commissions.get(record.symbol, Decimal("0")) + commission
        )
        return fill

    def all_orders_terminal(self, symbol: str) -> bool:
        records = [
            record
            for record in self.wal.recover_latest().values()
            if record.account_id == self.account_id and record.symbol == symbol
        ]
        return (
            not self.has_pending_position_update(symbol)
            and all(record.status in _TERMINAL for record in records)
        )

    def has_unresolved_orders(self) -> bool:
        return any(
            record.account_id == self.account_id
            and (record.record_type == "intent" or record.status == "SUBMIT_UNKNOWN")
            for record in self.wal.recover_latest().values()
        )

    def symbols_with_live_risk(self) -> set[str]:
        symbols = {
            symbol
            for symbol, position in self._positions.items()
            if position.quantity > 0
        }
        symbols.update(
            record.symbol
            for record in self.wal.recover_latest().values()
            if record.account_id == self.account_id and record.status not in _TERMINAL
        )
        return symbols

    def _apply_position(
        self,
        *,
        symbol: str,
        amount: Decimal,
        entry_price: Decimal,
        unrealized_pnl: Decimal,
        update_ms: int,
        position_side: str,
        confirms_stream_fill: bool,
    ) -> None:
        if (
            confirms_stream_fill
            and update_ms >= self._pending_position_update_ms.get(symbol, update_ms + 1)
        ):
            self._pending_position_update_ms.pop(symbol, None)
        if position_side not in {"BOTH", "SHORT", "LONG"}:
            raise ValueError(f"unknown position side: {position_side}")
        if amount == 0:
            self._positions.pop(symbol, None)
            self._position_update_ms[symbol] = update_ms
            self._commissions.pop(symbol, None)
            self.risk_guard.update_position(symbol, Decimal("0"))
            return
        is_short = amount < 0 or position_side == "SHORT"
        quantity = abs(amount)
        side = "SHORT" if is_short else "LONG"
        self._positions[symbol] = Position(
            symbol=symbol,
            side=side,
            entry_price=entry_price,
            quantity=quantity,
            total_commission=self._commissions.get(symbol, Decimal("0")),
            unrealized_pnl=unrealized_pnl,
            realized_pnl=Decimal("0"),
            opened_at=update_ms,
        )
        self._position_update_ms[symbol] = update_ms
        self.risk_guard.update_position(symbol, entry_price * quantity)

    def _to_order(self, record: OrderWALRecord) -> Order:
        payload = record.payload
        return Order(
            order_id=record.exchange_order_id or record.client_order_id,
            client_order_id=record.client_order_id,
            account_id=record.account_id,
            symbol=record.symbol,
            side=record.side,
            type=record.order_type,
            price=Decimal(record.price),
            quantity=Decimal(record.quantity),
            status=record.status or "SUBMIT_UNKNOWN",
            created_at=record.recorded_at,
            ttl_ms=payload.get("ttl_ms"),
            filled_quantity=self._filled_quantity(payload),
            strategy_id=payload.get("strategy_id"),
            trigger_reason=payload.get("trigger_reason"),
        )

    @staticmethod
    def _filled_quantity(payload: dict[str, Any]) -> Decimal:
        response = payload.get("exchange_response") or {}
        return Decimal(str(response.get("executedQty") or response.get("z") or "0"))

    @staticmethod
    def _decimal(data: dict[str, Any], key: str, *, default: str | None = None) -> Decimal:
        raw = data.get(key, default)
        if raw is None:
            raise ValueError(f"missing decimal field: {key}")
        try:
            value = Decimal(str(raw))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"invalid decimal field: {key}") from exc
        if not value.is_finite():
            raise ValueError(f"invalid decimal field: {key}")
        return value
