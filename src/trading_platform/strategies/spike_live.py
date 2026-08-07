"""Spike testnet/live 的安全执行编排。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any, Literal, NoReturn, Protocol

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from trading_platform.shared.binance.live_executor import BinanceOrderExecutor
from trading_platform.shared.binance.strategy_account import BinanceStrategyAccount
from trading_platform.shared.events import Bar1s, Fill, Kline, OrderIntent
from trading_platform.shared.risk import RiskGuard
from trading_platform.strategies.campaign_store import CampaignLease, RedisCampaignStore
from trading_platform.strategies.spike_short import (
    DynamicSpikeBacktestStrategy,
    parse_entry_client_order_id,
)


STRATEGY_ID = "spike_short"
ENTRY_REASONS = {"spike_tier1", "spike_tier2", "spike_tier3"}
LIVE_CONFIRMATION = "I_UNDERSTAND_LIVE_ORDERS_ARE_REAL"


def require_one_way_position_mode(response: dict[str, Any]) -> None:
    """V1 依赖 reduceOnly，拒绝 Binance Hedge Mode 或未知响应。"""
    if response.get("dualSidePosition") is not False:
        raise RuntimeError("Spike requires Binance one-way position mode")


class SpikeLiveSettings(BaseSettings):
    """进程安全配置；默认 testnet，live 需要固定短语二次确认。"""

    model_config = SettingsConfigDict(
        env_prefix="SPIKE_", extra="ignore", enable_decoding=False
    )

    mode: Literal["testnet", "live"] = "testnet"
    exit_policy: Literal["execution-test-d007", "candidate-v1"] = "candidate-v1"
    live_confirmation: str = ""
    account_id: str
    dedicated_strategy_account: bool = True
    symbols: list[str]
    total_notional: Decimal
    wal_path: str = "data/wal/spike_short.jsonl"
    subcategory: str = "spike"
    poll_interval_seconds: float = 5.0
    max_poll_attempts: int = 12

    @field_validator("symbols", mode="before")
    @classmethod
    def parse_symbols(cls, value):
        if isinstance(value, str):
            value = [part.strip().upper() for part in value.split(",") if part.strip()]
        return value

    @model_validator(mode="after")
    def validate_safety(self) -> "SpikeLiveSettings":
        if not self.account_id:
            raise ValueError("account_id is required")
        if not self.dedicated_strategy_account:
            raise ValueError("Spike requires a dedicated strategy account")
        if not self.symbols or any(not symbol.endswith("USDT") for symbol in self.symbols):
            raise ValueError("at least one USDT symbol is required")
        if self.total_notional <= 0:
            raise ValueError("total_notional must be positive")
        if self.poll_interval_seconds != 5 or self.max_poll_attempts != 12:
            raise ValueError("SUBMIT_UNKNOWN recovery is frozen at 5s x 12")
        if self.mode == "live":
            if self.live_confirmation != LIVE_CONFIRMATION:
                raise ValueError("live mode requires the exact live confirmation phrase")
            raise ValueError(
                "live mode requires the latest exit policy to be calibrated and frozen"
            )
        return self


class CompositeEntryGate:
    """所有安全条件均为真时才打开策略入场；未知条件默认关闭。"""

    def __init__(self, strategy: DynamicSpikeBacktestStrategy):
        self.strategy = strategy
        self._conditions: dict[str, bool] = {}

    @property
    def enabled(self) -> bool:
        return bool(self._conditions) and all(self._conditions.values())

    def set_condition(self, name: str, enabled: bool) -> None:
        if not name:
            raise ValueError("gate condition name is required")
        self._conditions[name] = bool(enabled)
        if name == "execution":
            setter = getattr(self.strategy, "set_execution_enabled", None)
            if callable(setter):
                setter(bool(enabled))
        self.strategy.set_entry_enabled(self.enabled)

    def condition(self, name: str) -> bool:
        return self._conditions.get(name, False)

    def admission_view(self) -> "NamedEntryGate":
        return NamedEntryGate(self, "subcategory")


@dataclass(frozen=True)
class NamedEntryGate:
    gate: CompositeEntryGate
    name: str

    def set_entry_enabled(self, enabled: bool) -> None:
        self.gate.set_condition(self.name, enabled)


class AuditSink(Protocol):
    async def __call__(self, events: tuple[object, ...]) -> None:
        ...


class CampaignTradeSource(Protocol):
    async def get_trades_by_client_order_ids(
        self,
        *,
        account_id: str,
        strategy_id: str,
        symbol: str,
        client_order_ids: list[str],
    ) -> list[Any]:
        ...


class SpikeExecutionCoordinator:
    """串行处理市场事件、Campaign 互斥、风险检查与可靠订单提交。"""

    def __init__(
        self,
        *,
        strategy: DynamicSpikeBacktestStrategy,
        account: BinanceStrategyAccount,
        executor: BinanceOrderExecutor,
        campaign_store: RedisCampaignStore,
        risk_guard: RiskGuard,
        gate: CompositeEntryGate,
        account_id: str,
        trade_source: CampaignTradeSource | None = None,
        audit_sink: AuditSink | None = None,
        now_ms: Callable[[], int] | None = None,
    ):
        self.strategy = strategy
        self.account = account
        self.executor = executor
        self.campaign_store = campaign_store
        self.risk_guard = risk_guard
        self.gate = gate
        self.account_id = account_id
        self.trade_source = trade_source
        self.audit_sink = audit_sink
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))
        self._lock = asyncio.Lock()
        self._owned_campaign_id: str | None = None
        self._owned_campaign_lease: CampaignLease | None = None
        self._expiry_tasks: dict[str, asyncio.Task] = {}

    async def restore_campaign_gate(self) -> None:
        """恢复 Redis 互斥事实；不依据本地状态猜测或删除已有 lease。"""
        previous_campaign_id = self._owned_campaign_id
        lease = await self.campaign_store.get_active()
        if lease is None:
            live_risk = self.account.symbols_with_live_risk()
            if previous_campaign_id is not None or live_risk:
                self.gate.set_condition("campaign", False)
                self.risk_guard.halt("Redis Campaign disappeared while risk remains")
                raise RuntimeError("Redis Campaign disappeared while risk remains")
            self._owned_campaign_id = None
            self._owned_campaign_lease = None
            self.gate.set_condition("campaign", True)
            return
        self._owned_campaign_id = None
        self._owned_campaign_lease = None
        if lease.strategy_id == STRATEGY_ID and lease.symbol in self.strategy.strategies:
            self._owned_campaign_id = lease.campaign_id
            self._owned_campaign_lease = lease
        self.gate.set_condition("campaign", self._owned_campaign_id is not None)

    def validate_recovered_campaign(self) -> None:
        """已有交易风险必须有本进程可识别的 Redis Campaign，禁止猜测恢复。"""
        live_symbols = self.account.symbols_with_live_risk()
        if not live_symbols:
            return
        campaign_id = self._owned_campaign_id
        if campaign_id is None:
            self.gate.set_condition("campaign", False)
            raise RuntimeError(
                "live orders or positions exist without an owned Redis Campaign"
            )
        parts = campaign_id.split(":")
        if len(parts) != 3 or parts[0] != STRATEGY_ID or parts[1] not in live_symbols:
            self.gate.set_condition("campaign", False)
            raise RuntimeError("Redis Campaign does not match recovered live risk")
        if live_symbols != {parts[1]}:
            self.gate.set_condition("campaign", False)
            raise RuntimeError("recovered live risk spans multiple symbols")

    async def restore_campaign_timing(self) -> None:
        """用 Redis Campaign、WAL 身份和 PostgreSQL 成交恢复带仓计时。"""
        positioned = {
            symbol
            for symbol in self.account.symbols_with_live_risk()
            if self.account.get_position(symbol) is not None
        }
        if not positioned:
            lease = self._owned_campaign_lease
            if lease is not None:
                self.strategy.restore_pending_campaign(
                    lease.symbol,
                    lease.campaign_id,
                    origin_price=(
                        None
                        if lease.origin_price is None
                        else Decimal(lease.origin_price)
                    ),
                )
            return
        self.validate_recovered_campaign()
        campaign_id = self._owned_campaign_id
        assert campaign_id is not None
        _, symbol, raw_signal_time = campaign_id.split(":")
        try:
            signal_time = int(raw_signal_time)
        except ValueError:
            self._fail_campaign_recovery("recovered Campaign has invalid signal time")

        latest = self.account.wal.recover_latest().values()
        owned_records = [
            record
            for record in latest
            if record.account_id == self.account_id
            and record.symbol == symbol
            and record.recorded_at >= signal_time
            and record.payload.get("strategy_id") == STRATEGY_ID
        ]
        records_by_client_id = {
            record.client_order_id: record for record in owned_records
        }
        entry_ids = {
            record.client_order_id
            for record in owned_records
            if not bool(record.payload.get("reduce_only", False))
            and parse_entry_client_order_id(
                record.client_order_id, expected_symbol=symbol
            )
            == (symbol, signal_time)
        }
        if not entry_ids:
            self._fail_campaign_recovery(
                "recovered Campaign has no matching WAL entry orders"
            )
        if self.trade_source is None:
            self._fail_campaign_recovery("PostgreSQL Campaign trade source is unavailable")

        client_order_ids = sorted(
            {record.client_order_id for record in owned_records}
        )
        trades = await self.trade_source.get_trades_by_client_order_ids(
            account_id=self.account_id,
            strategy_id=STRATEGY_ID,
            symbol=symbol,
            client_order_ids=client_order_ids,
        )
        if any(trade.client_order_id not in client_order_ids for trade in trades):
            self._fail_campaign_recovery("PostgreSQL returned a trade outside Campaign WAL")
        entry_trades = [
            trade
            for trade in trades
            if trade.client_order_id in entry_ids and trade.side == "SELL"
        ]
        if not entry_trades:
            self._fail_campaign_recovery(
                "recovered Campaign has no PostgreSQL entry trade"
            )
        if any(trade.exchange_time is None for trade in trades):
            self._fail_campaign_recovery("recovered Campaign trade has no exchange time")

        filled_exit_reasons = {
            records_by_client_id[trade.client_order_id].payload.get("trigger_reason")
            for trade in trades
            if trade.client_order_id in records_by_client_id
            and trade.side == "BUY"
            and trade.quantity > 0
        }

        first_fill_time = min(
            round(trade.exchange_time.timestamp() * 1000) for trade in entry_trades
        )
        total_commission = sum(
            (trade.commission for trade in trades), start=Decimal("0")
        )
        self.account.restore_trade_state(
            symbol,
            total_commission,
            {str(trade.trade_id) for trade in trades},
        )
        lease = self._owned_campaign_lease
        wal_reasons = {
            record.payload.get("trigger_reason") for record in owned_records
        }
        reduced_at_origin = "candidate_origin_reduce" in filled_exit_reasons
        full_exit_reasons = {
            "candidate_time_risk_exit",
            "candidate_momentum_exit",
            "candidate_trend_exit",
        }
        filled_full_exit = bool(filled_exit_reasons & full_exit_reasons)
        exit_requested = any(
            record.payload.get("trigger_reason") in full_exit_reasons
            and record.status not in {"FILLED", "CANCELLED", "EXPIRED"}
            for record in owned_records
        )
        if lease is not None and lease.reduced_at_origin:
            if "candidate_origin_reduce" not in wal_reasons:
                self._fail_campaign_recovery(
                    "Redis candidate exit state has no matching WAL order"
                )
            if not reduced_at_origin:
                self._fail_campaign_recovery(
                    "Redis candidate reduction has no matching actual trade"
                )
        if lease is not None and lease.exit_requested:
            if not wal_reasons & full_exit_reasons:
                self._fail_campaign_recovery(
                    "Redis candidate exit state has no matching WAL order"
                )
            if not filled_full_exit:
                self._fail_campaign_recovery(
                    "Redis candidate exit has no matching actual trade"
                )
        self.strategy.restore_campaign_timing(
            symbol,
            campaign_id,
            first_fill_time,
            origin_price=(
                None
                if lease is None or lease.origin_price is None
                else Decimal(lease.origin_price)
            ),
            origin_checked=(
                reduced_at_origin
                or (False if lease is None else lease.origin_checked)
            ),
            reduced_at_origin=reduced_at_origin,
            exit_requested=exit_requested,
        )

    def _fail_campaign_recovery(self, message: str) -> NoReturn:
        self.gate.set_condition("campaign", False)
        raise RuntimeError(message)

    async def on_bar1s(self, bar: Bar1s) -> None:
        async with self._lock:
            intents = self.strategy.on_bar1s(bar)
            execution_complete = await self._execute(
                intents, event_time=bar.available_time
            )
            if execution_complete:
                await self._persist_exit_state(bar.symbol)
            await self._flush_cancellations()
            await self._publish_audit()
            await self.maybe_release_campaign(bar.symbol)

    async def on_kline(self, kline: Kline) -> None:
        async with self._lock:
            intents = self.strategy.on_kline(kline)
            execution_complete = await self._execute(
                intents, event_time=kline.available_time
            )
            if execution_complete:
                await self._persist_exit_state(kline.symbol)
            await self._flush_cancellations()
            await self._publish_audit()

    async def on_fill(self, fill: Fill) -> None:
        async with self._lock:
            self.strategy.on_fill(fill)
            await self._publish_audit()

    async def reconcile_entry_expirations(self) -> None:
        """从 WAL 恢复未终态入场单 TTL；过期订单立即进入撤单流程。"""
        cancel_due = False
        now_ms = self._now_ms()
        for order in self.account.iter_orders():
            if (
                order.reduce_only
                or order.status not in {"NEW", "PARTIALLY_FILLED"}
                or order.ttl_ms is None
                or order.ttl_ms <= 0
            ):
                continue
            expires_at = order.created_at + order.ttl_ms
            remaining_seconds = max(0, expires_at - now_ms) / 1000
            if remaining_seconds == 0:
                cancel_due = self.account.cancel_order(order.order_id) or cancel_due
                continue
            task = self._expiry_tasks.get(order.client_order_id)
            if task is None or task.done():
                self._expiry_tasks[order.client_order_id] = asyncio.create_task(
                    self._expire_order(order.client_order_id, remaining_seconds)
                )
        if cancel_due:
            await self._flush_cancellations()

    async def _execute(self, intents: list[OrderIntent], *, event_time: int) -> bool:
        entries = [intent for intent in intents if not intent.reduce_only]
        exits = [intent for intent in intents if intent.reduce_only]
        if entries and self.gate.enabled:
            campaign_id = self._campaign_id(entries[0])
            if not await self._acquire_campaign(campaign_id, entries[0].symbol, event_time):
                self.gate.set_condition("campaign", False)
            else:
                total_value = sum(intent.price * intent.quantity for intent in entries)
                allowed, reason = self.risk_guard.check_can_open(
                    entries[0].symbol, total_value
                )
                if not allowed:
                    self.risk_guard.block_symbol(
                        entries[0].symbol, f"entry rejected:{reason}"
                    )
                else:
                    for intent in entries:
                        await self._submit(intent)
        for intent in exits:
            if not self.gate.condition("execution"):
                self.risk_guard.halt("exit blocked while execution facts are unavailable")
                return False
            await self._submit(intent)
        return True

    async def _submit(self, intent: OrderIntent) -> None:
        record = await self.executor.submit(
            intent,
            reference_price=intent.price,
        )
        if record.status == "SUBMIT_UNKNOWN":
            self.gate.set_condition("execution", False)
            self.risk_guard.halt("submit status unknown")
        if intent.ttl_ms is not None and intent.ttl_ms > 0:
            task = self._expiry_tasks.get(intent.client_order_id)
            if task is None or task.done():
                self._expiry_tasks[intent.client_order_id] = asyncio.create_task(
                    self._expire_order(intent.client_order_id, intent.ttl_ms / 1000)
                )

    async def _expire_order(self, client_order_id: str, delay_seconds: float) -> None:
        try:
            await asyncio.sleep(delay_seconds)
            order = next(
                (
                    order
                    for order in self.account.iter_orders()
                    if order.client_order_id == client_order_id
                ),
                None,
            )
            if order is not None and order.status in {"NEW", "PARTIALLY_FILLED"}:
                self.account.cancel_order(order.order_id)
                await self._flush_cancellations()
                await self.maybe_release_campaign(order.symbol)
        finally:
            self._expiry_tasks.pop(client_order_id, None)

    async def _acquire_campaign(
        self, campaign_id: str, symbol: str, event_time: int
    ) -> bool:
        if self._owned_campaign_id == campaign_id:
            return True
        active = await self.campaign_store.get_active()
        if active is not None:
            if active.campaign_id == campaign_id and active.strategy_id == STRATEGY_ID:
                self._owned_campaign_id = campaign_id
                self._owned_campaign_lease = active
                return True
            return False
        origin_getter = getattr(self.strategy, "campaign_origin_price", None)
        origin_price = (
            origin_getter(campaign_id) if callable(origin_getter) else None
        )
        lease = CampaignLease(
            campaign_id,
            STRATEGY_ID,
            symbol,
            event_time,
            origin_price=None if origin_price is None else str(origin_price),
        )
        acquired = await self.campaign_store.acquire(lease)
        if acquired:
            self._owned_campaign_id = campaign_id
            self._owned_campaign_lease = lease
        return acquired

    async def _persist_exit_state(self, symbol: str) -> None:
        lease = self._owned_campaign_lease
        if lease is None or lease.symbol != symbol:
            return
        state_getter = getattr(self.strategy, "campaign_exit_state", None)
        if not callable(state_getter):
            return
        state = state_getter(symbol)
        if state is None:
            return
        origin_checked, reduced_at_origin, exit_requested = state
        if state == (
            lease.origin_checked,
            lease.reduced_at_origin,
            lease.exit_requested,
        ):
            return
        updated = await self.campaign_store.update_exit_state(
            lease.campaign_id,
            origin_checked=origin_checked,
            reduced_at_origin=reduced_at_origin,
            exit_requested=exit_requested,
        )
        if not updated:
            self.gate.set_condition("campaign", False)
            raise RuntimeError("failed to persist candidate exit state")
        self._owned_campaign_lease = replace(
            lease,
            origin_checked=origin_checked,
            reduced_at_origin=reduced_at_origin,
            exit_requested=exit_requested,
        )

    async def maybe_release_campaign(self, symbol: str) -> bool:
        campaign_id = self._owned_campaign_id
        if campaign_id is None:
            return False
        if self.account.has_open_position(symbol):
            return False
        # An execution report can precede ACCOUNT_UPDATE. Keep the Campaign
        # lease until the position fact confirms the fill, even if the WAL is
        # already terminal.
        has_pending_position_update = getattr(
            self.account, "has_pending_position_update", None
        )
        if has_pending_position_update is not None and has_pending_position_update(symbol):
            return False
        if not self.account.all_orders_terminal(symbol):
            return False
        released = await self.campaign_store.release(campaign_id)
        if released:
            self._owned_campaign_id = None
            self._owned_campaign_lease = None
            self.gate.set_condition("campaign", True)
        return released

    async def stop(self) -> None:
        for task in tuple(self._expiry_tasks.values()):
            task.cancel()
        if self._expiry_tasks:
            await asyncio.gather(*self._expiry_tasks.values(), return_exceptions=True)
        self._expiry_tasks.clear()
        for order in self.account.iter_orders():
            if (
                not order.reduce_only
                and order.status in {"NEW", "PARTIALLY_FILLED"}
            ):
                self.account.cancel_order(order.order_id)
        await self._flush_cancellations()
        remaining = [
            order.client_order_id
            for order in self.account.iter_orders()
            if not order.reduce_only
            and order.status in {"NEW", "PARTIALLY_FILLED"}
        ]
        if remaining:
            self.gate.set_condition("execution", False)
            raise RuntimeError(f"entry orders remain open during shutdown: {remaining}")

    async def _flush_cancellations(self) -> None:
        cancelled = await self.account.flush_cancellations()
        if self.account.has_pending_cancellations:
            self.risk_guard.halt("entry cancellation unresolved")
            self.gate.set_condition("execution", False)
            return
        refresh_positions = getattr(self.account, "refresh_positions", None)
        if cancelled and callable(refresh_positions):
            await refresh_positions()

    async def _publish_audit(self) -> None:
        events = tuple(self.strategy.drain_audit_events())
        if events and self.audit_sink is not None:
            await self.audit_sink(events)

    @staticmethod
    def _campaign_id(intent: OrderIntent) -> str:
        parsed = parse_entry_client_order_id(
            intent.client_order_id, expected_symbol=intent.symbol
        )
        if parsed is None:
            raise ValueError("invalid Spike entry client_order_id")
        symbol, signal_time = parsed
        return f"{STRATEGY_ID}:{symbol}:{signal_time}"


class BinanceCallbackDelegate(Protocol):
    async def handle_execution_report(self, order_data: dict[str, Any]) -> None:
        ...

    async def handle_account_update(self, event: dict[str, Any]) -> None:
        ...


class SpikeRuntimeCallbacks:
    """在账本/WAL 回调后更新策略视图，任一失败立即关闭执行准入。"""

    def __init__(
        self,
        *,
        delegate: BinanceCallbackDelegate,
        account: BinanceStrategyAccount,
        coordinator: SpikeExecutionCoordinator,
        gate: CompositeEntryGate,
        risk_guard: RiskGuard | None = None,
    ):
        self.delegate = delegate
        self.account = account
        self.coordinator = coordinator
        self.gate = gate
        self.risk_guard = risk_guard
        self._startup_recovery_ready = asyncio.Event()
        self._startup_recovery_ready.set()
        self._startup_recovery_failed = False

    def begin_startup_recovery(self) -> None:
        self._startup_recovery_failed = False
        self._startup_recovery_ready.clear()

    def finish_startup_recovery(self) -> None:
        self._startup_recovery_ready.set()

    def abort_startup_recovery(self) -> None:
        self._startup_recovery_failed = True
        self._startup_recovery_ready.set()

    async def _wait_for_startup_recovery(self) -> None:
        await self._startup_recovery_ready.wait()
        if self._startup_recovery_failed:
            raise RuntimeError("Spike startup recovery failed")

    async def handle_execution_report(self, order_data: dict[str, Any]) -> None:
        try:
            await self._wait_for_startup_recovery()
            await self.delegate.handle_execution_report(order_data)
            fill = self.account.handle_execution_report(order_data)
            if fill is not None:
                await self.coordinator.on_fill(fill)
            await self.coordinator.reconcile_entry_expirations()
            symbol = str(order_data.get("s") or "")
            if symbol:
                await self.coordinator.maybe_release_campaign(symbol)
        except BaseException:
            self.gate.set_condition("execution", False)
            if self.risk_guard is not None:
                self.risk_guard.halt("execution report handling failed")
            raise

    async def handle_account_update(self, event: dict[str, Any]) -> None:
        try:
            await self._wait_for_startup_recovery()
            await self.delegate.handle_account_update(event)
            await self.account.handle_account_update(event)
            positions = event.get("a", {}).get("P", [])
            for symbol in {str(item.get("s") or "") for item in positions} - {""}:
                await self.coordinator.maybe_release_campaign(symbol)
        except BaseException:
            self.gate.set_condition("execution", False)
            if self.risk_guard is not None:
                self.risk_guard.halt("account update handling failed")
            raise
