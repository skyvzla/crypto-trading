"""Spike testnet/live 的安全执行编排。"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal, NoReturn, Protocol

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from trading_platform.shared.binance.live_executor import BinanceOrderExecutor
from trading_platform.shared.binance.strategy_account import BinanceStrategyAccount
from trading_platform.shared.events import (
    Bar1s,
    Fill,
    Kline,
    OrderIntent,
    StrategyAuditEvent,
)
from trading_platform.shared.risk import RiskGuard
from trading_platform.strategies.campaign_store import CampaignLease, RedisCampaignStore
from trading_platform.strategies.spike.execution_queue import (
    ExecutionJob,
    ExecutionQueue,
    ExecutionWorker,
)
from trading_platform.strategies.spike.capital import CapitalPolicy, CapitalPolicyConfig
from trading_platform.strategies.spike.capital_store import CapitalSnapshot
from trading_platform.strategies.spike.signal_arbiter import SignalArbiter
from trading_platform.strategies.spike.short import (
    DynamicSpikeBacktestStrategy,
    parse_entry_client_order_id,
)
from trading_platform.strategies.universe import DEFAULT_DELISTING_FREEZE_DAYS


STRATEGY_ID = "spike_short"
ENTRY_REASONS = {"spike_entry", "spike_tier1", "spike_tier2", "spike_tier3"}
LIVE_CONFIRMATION = "I_UNDERSTAND_LIVE_ORDERS_ARE_REAL"


logger = logging.getLogger(__name__)


def campaign_store_key(account_id: str, strategy_id: str) -> str:
    """账户和策略共同定义唯一 Campaign 命名空间。"""

    if not account_id or not strategy_id:
        raise ValueError("account_id and strategy_id are required")
    return f"trading_platform:campaign:{account_id}:{strategy_id}:active"


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
    strategy_path: str = "trading_platform.strategies.spike.v1:V1"
    exit_policy: Literal["execution-test-d007", "candidate-v1"] = "candidate-v1"
    live_confirmation: str = ""
    account_id: str
    dedicated_strategy_account: bool = True
    symbols: list[str]
    total_notional: Decimal | None = None
    initial_account_capital: Decimal | None = None
    initial_trading_capital: Decimal | None = None
    profit_reinvest_ratio: Decimal | None = None
    minimum_trading_capital: Decimal | None = None
    entry_tier_mode: Literal["three-tier", "tier3-only", "single-entry"] = (
        "single-entry"
    )
    wal_path: str = "data/wal/spike_short.jsonl"
    subcategory: str = "spike"
    poll_interval_seconds: float = 5.0
    max_poll_attempts: int = 12
    delisting_freeze_days: int = DEFAULT_DELISTING_FREEZE_DAYS

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
        formal = (
            self.initial_account_capital,
            self.initial_trading_capital,
            self.profit_reinvest_ratio,
            self.minimum_trading_capital,
        )
        if any(value is not None for value in formal):
            if any(value is None for value in formal):
                raise ValueError(
                    "capital policy fields must be configured together"
                )
            CapitalPolicyConfig(
                initial_account_capital=formal[0],
                initial_trading_capital=formal[1],
                profit_reinvest_ratio=formal[2],
                minimum_trading_capital=formal[3],
            )
        elif self.total_notional is None or self.total_notional <= 0:
            raise ValueError(
                "formal capital policy or positive total_notional is required"
            )
        if self.entry_tier_mode != "single-entry":
            raise ValueError("Spike live entry_tier_mode must be single-entry")
        if self.poll_interval_seconds != 5 or self.max_poll_attempts != 12:
            raise ValueError("SUBMIT_UNKNOWN recovery is frozen at 5s x 12")
        if self.delisting_freeze_days < 0:
            raise ValueError("delisting_freeze_days must be non-negative")
        if self.mode == "live":
            if self.live_confirmation != LIVE_CONFIRMATION:
                raise ValueError("live mode requires the exact live confirmation phrase")
            raise ValueError(
                "live mode requires the latest exit policy to be calibrated and frozen"
            )
        return self

    @property
    def capital_config(self) -> CapitalPolicyConfig:
        if self.initial_account_capital is not None:
            return CapitalPolicyConfig(
                initial_account_capital=self.initial_account_capital,
                initial_trading_capital=self.initial_trading_capital,
                profit_reinvest_ratio=self.profit_reinvest_ratio,
                minimum_trading_capital=self.minimum_trading_capital,
            )
        assert self.total_notional is not None
        return CapitalPolicyConfig(
            initial_account_capital=self.total_notional,
            initial_trading_capital=self.total_notional,
            profit_reinvest_ratio=Decimal("1"),
            minimum_trading_capital=Decimal("0"),
        )

    @property
    def initial_order_notional(self) -> Decimal:
        return self.capital_config.initial_trading_capital


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

    def snapshot(self) -> dict[str, bool]:
        return dict(self._conditions)

    def admission_view(self) -> "NamedEntryGate":
        return NamedEntryGate(self, "subcategory")


@dataclass(frozen=True)
class NamedEntryGate:
    gate: CompositeEntryGate
    name: str

    def set_entry_enabled(self, enabled: bool) -> None:
        self.gate.set_condition(self.name, enabled)


class AuditSink(Protocol):
    async def __call__(self, events: tuple[StrategyAuditEvent, ...]) -> object:
        ...


class CampaignTradeSource(Protocol):
    async def get_trades_by_client_order_ids(
        self,
        *,
        account_id: str,
        strategy_id: str,
        symbol: str,
        campaign_id: str,
        client_order_ids: list[str],
    ) -> list[Any]:
        ...

    async def get_campaign_pnl(
        self,
        *,
        account_id: str,
        strategy_id: str,
        campaign_id: str,
    ) -> Any | None:
        ...


class CapitalSettlementStore(Protocol):
    async def settle(self, **kwargs: Any) -> Any:
        ...


class CampaignFundingSource(Protocol):
    async def sync_funding_fee_total(
        self,
        *,
        account_id: str,
        symbol: str,
        start_at: datetime,
        end_at: datetime,
    ) -> Decimal:
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
        execution_queue: ExecutionQueue | None = None,
        capital_store: CapitalSettlementStore | None = None,
        funding_source: CampaignFundingSource | None = None,
        capital_admission_refresh: Callable[
            [CapitalSnapshot], Awaitable[bool]
        ]
        | None = None,
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
        self.capital_store = capital_store
        self.funding_source = funding_source
        self.capital_admission_refresh = capital_admission_refresh
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))
        self._lock = asyncio.Lock()
        self._campaign_lock = asyncio.Lock()
        self._owned_campaign_id: str | None = None
        self._owned_campaign_lease: CampaignLease | None = None
        self._submissions_inflight = 0
        self._signal_arbiter = SignalArbiter()
        self._expiry_tasks: dict[str, asyncio.Task] = {}
        self._pending_audit_events: tuple[StrategyAuditEvent, ...] = ()
        self._audit_lock = asyncio.Lock()
        self.execution_queue = execution_queue or ExecutionQueue()
        self._execution_worker = ExecutionWorker(
            self.execution_queue, self._handle_execution_job
        )
        self._maintenance_queued = False
        self._entry_pipeline_close_reason: str | None = None

    def start_execution_worker(self) -> asyncio.Task[None]:
        """启动唯一账户执行 worker，并把任务交给进程监督。"""

        return self._execution_worker.start()

    async def stop_execution_worker(self) -> None:
        await self._execution_worker.stop()

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
            self._signal_arbiter.restore_active(lease.campaign_id)
            self._queue_campaign_audit(
                "campaign_recovered",
                lease,
                event_time=self._now_ms(),
            )
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
            and record.payload.get("strategy_id") == STRATEGY_ID
            and record.payload.get("campaign_id") == campaign_id
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
            campaign_id=campaign_id,
            client_order_ids=client_order_ids,
        )
        if any(trade.campaign_id != campaign_id for trade in trades):
            self._fail_campaign_recovery(
                "PostgreSQL returned a trade outside the owned Campaign"
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
            and record.status not in {"FILLED", "CANCELLED", "EXPIRED", "REJECTED"}
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
            entry_bucket=None if lease is None else lease.entry_bucket,
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

    async def on_bar1s_queued(self, bar: Bar1s) -> None:
        """只计算策略并排队执行，不在策略事件循环中等待交易所 REST。"""

        async with self._lock:
            intents = self.strategy.on_bar1s(bar)
            queued = self._enqueue_intents(intents, event_time=bar.available_time)
            audit_pending = await self._stage_strategy_audit_events()
            if not queued and (
                audit_pending or self.account.has_pending_cancellations
            ):
                self._enqueue_maintenance(event_time=bar.available_time)

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

    async def on_kline_queued(self, kline: Kline) -> None:
        """与 1s Bar 共用同一个异步执行通道。"""

        async with self._lock:
            intents = self.strategy.on_kline(kline)
            queued = self._enqueue_intents(intents, event_time=kline.available_time)
            audit_pending = await self._stage_strategy_audit_events()
            if not queued and (
                audit_pending or self.account.has_pending_cancellations
            ):
                self._enqueue_maintenance(event_time=kline.available_time)

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

    async def update_exchange_symbol_admission(
        self,
        allowed_symbols: set[str] | frozenset[str],
        *,
        symbol_rules: object | None = None,
    ) -> frozenset[str]:
        """Atomically block entries and cancel open entry orders by symbol."""

        async with self._lock:
            if symbol_rules is not None:
                self.executor.symbol_rules = symbol_rules
            managed = frozenset(self.strategy.strategies)
            allowed = frozenset(
                symbol.strip().upper()
                for symbol in allowed_symbols
                if symbol.strip().upper() in managed
            )
            blocked = managed - allowed
            self.strategy.set_blocked_entry_symbols(blocked)
            await self._cancel_blocked_entry_orders(blocked)
            return blocked

    async def cancel_open_entry_orders(
        self, symbols: set[str] | frozenset[str] | None = None
    ) -> None:
        """关闭行情准入时撤销入场单，不触碰 reduce-only 风险退出单。"""
        async with self._lock:
            selected = None if symbols is None else frozenset(symbols)
            cancellation_requested = False
            for order in self.account.iter_orders():
                if (
                    not order.reduce_only
                    and order.status in {"NEW", "PARTIALLY_FILLED"}
                    and (selected is None or order.symbol in selected)
                ):
                    cancellation_requested = (
                        self.account.cancel_order(order.order_id)
                        or cancellation_requested
                    )
            if cancellation_requested:
                await self._flush_cancellations()

    async def reconcile_exchange_symbol_admission(self) -> None:
        """Recheck orders that just changed from unknown to a cancellable state."""

        async with self._lock:
            await self._cancel_blocked_entry_orders(
                self.strategy.blocked_entry_symbols
            )

    async def _cancel_blocked_entry_orders(
        self, blocked: frozenset[str]
    ) -> None:
        cancellation_requested = False
        for order in self.account.iter_orders():
            if (
                order.symbol in blocked
                and not order.reduce_only
                and order.status in {"NEW", "PARTIALLY_FILLED"}
            ):
                cancellation_requested = (
                    self.account.cancel_order(order.order_id)
                    or cancellation_requested
                )
        if cancellation_requested:
            await self._flush_cancellations()

    async def _execute(
        self,
        intents: list[OrderIntent],
        *,
        event_time: int,
        require_arbitrated: bool = False,
    ) -> bool:
        symbol_allowed = getattr(self.strategy, "is_symbol_entry_enabled", None)
        entries = [
            intent
            for intent in intents
            if not intent.reduce_only
            and (not callable(symbol_allowed) or symbol_allowed(intent.symbol))
        ]
        exits = [intent for intent in intents if intent.reduce_only]
        approved_entries: list[OrderIntent] = []
        if entries and self.gate.enabled:
            campaign_id = self._campaign_id(entries[0])
            async with self._campaign_lock:
                if (
                    require_arbitrated
                    and self._signal_arbiter.active_campaign_id != campaign_id
                ):
                    entries = []
                elif not await self._acquire_campaign(
                    campaign_id, entries[0].symbol, event_time
                ):
                    self.gate.set_condition("campaign", False)
                    if self._signal_arbiter.active_campaign_id == campaign_id:
                        self._signal_arbiter.release(campaign_id)
                else:
                    total_value = sum(
                        intent.price * intent.quantity for intent in entries
                    )
                    allowed, reason = self.risk_guard.check_can_open(
                        entries[0].symbol, total_value
                    )
                    if not allowed:
                        self.risk_guard.block_symbol(
                            entries[0].symbol, f"entry rejected:{reason}"
                        )
                    else:
                        approved_entries = entries
                        self._submissions_inflight += len(approved_entries)
        try:
            for intent in approved_entries:
                await self._submit(intent)
        finally:
            if approved_entries:
                async with self._campaign_lock:
                    self._submissions_inflight -= len(approved_entries)

        approved_exits: list[OrderIntent] = []
        for intent in exits:
            if not self.gate.condition("execution"):
                self.risk_guard.halt("exit blocked while execution facts are unavailable")
                return False
            approved_exits.append(intent)
        if approved_exits:
            async with self._campaign_lock:
                self._submissions_inflight += len(approved_exits)
            try:
                for intent in approved_exits:
                    await self._submit(intent)
            finally:
                async with self._campaign_lock:
                    self._submissions_inflight -= len(approved_exits)
        return True

    def _enqueue_intents(
        self, intents: list[OrderIntent], *, event_time: int
    ) -> int:
        """退出永不因入场积压被拒；同优先级按策略事件到达顺序排队。"""

        symbol_allowed = getattr(self.strategy, "is_symbol_entry_enabled", None)
        entries = [
            intent
            for intent in intents
            if not intent.reduce_only
            and (not callable(symbol_allowed) or symbol_allowed(intent.symbol))
        ]
        exits = [intent for intent in intents if intent.reduce_only]
        queued = 0
        for intent in exits:
            self.execution_queue.put_nowait(
                "exit", intent=intent, event_time=event_time
            )
            queued += 1
        campaigns: dict[str, list[OrderIntent]] = {}
        for intent in entries:
            campaigns.setdefault(self._campaign_id(intent), []).append(intent)
        for campaign_id, campaign_entries in campaigns.items():
            if not self.gate.enabled:
                continue
            symbol, signal_time = parse_entry_client_order_id(
                campaign_entries[0].client_order_id,
                expected_symbol=campaign_entries[0].symbol,
            ) or ("", -1)
            candidate = self._signal_arbiter.enqueue(
                symbol=symbol,
                campaign_id=campaign_id,
                signal_time=signal_time,
                received_at=event_time,
            )
            result = self._signal_arbiter.arbitrate(now_ms=event_time)[0]
            if result.status != "acquired":
                self._pending_audit_events += (
                    StrategyAuditEvent(
                        event_time=event_time,
                        event_type=f"signal_{result.status}",
                        symbol=candidate.symbol,
                        strategy_id=STRATEGY_ID,
                        campaign_id=candidate.campaign_id,
                        details={"arrival_sequence": candidate.arrival_sequence},
                    ),
                )
                continue
            for intent in campaign_entries:
                try:
                    self.execution_queue.put_nowait(
                        "entry", intent=intent, event_time=event_time
                    )
                except asyncio.QueueFull:
                    self.close_entry_pipeline(
                        "entry execution queue full",
                        symbol=intent.symbol,
                        event_time=event_time,
                    )
                    continue
                queued += 1
        return queued

    def _enqueue_maintenance(self, *, event_time: int) -> None:
        if self._maintenance_queued:
            return
        self.execution_queue.put_nowait("cancel", event_time=event_time)
        self._maintenance_queued = True

    def close_entry_pipeline(
        self, reason: str, *, symbol: str, event_time: int
    ) -> None:
        """永久关闭本次运行的入场，并把现有入场挂单交给执行 worker 撤销。"""

        self.gate.set_condition("event_queue", False)
        if self._entry_pipeline_close_reason is None:
            self._entry_pipeline_close_reason = reason
            logger.error("%s entry pipeline closed: %s", symbol, reason)
            self._pending_audit_events += (
                StrategyAuditEvent(
                    event_time=event_time,
                    event_type="entry_pipeline_closed",
                    symbol=symbol,
                    strategy_id=STRATEGY_ID,
                    campaign_id=self._owned_campaign_id,
                    details={"reason": reason},
                ),
            )
        cancellation_requested = False
        for order in self.account.iter_orders():
            if not order.reduce_only and order.status in {"NEW", "PARTIALLY_FILLED"}:
                cancellation_requested = (
                    self.account.cancel_order(order.order_id)
                    or cancellation_requested
                )
        if cancellation_requested or self._pending_audit_events:
            self._enqueue_maintenance(event_time=event_time)

    async def _handle_execution_job(self, job: ExecutionJob) -> None:
        if job.kind == "cancel":
            self._maintenance_queued = False
        try:
            execution_complete = True
            if job.kind == "cancel":
                await self._flush_cancellations()
            else:
                assert job.intent is not None
                execution_complete = await self._execute(
                    [job.intent],
                    event_time=job.event_time,
                    require_arbitrated=job.kind == "entry",
                )
                if execution_complete and job.intent.reduce_only:
                    await self._persist_exit_state(job.intent.symbol)
                await self._flush_cancellations()
            await self._publish_audit()
            if job.intent is not None:
                await self.maybe_release_campaign(job.intent.symbol)
        except asyncio.CancelledError:
            raise
        except BaseException:
            self.gate.set_condition("event_queue", False)
            raise

    async def _submit(self, intent: OrderIntent) -> None:
        campaign_id = self._owned_campaign_id
        if campaign_id is None:
            self.gate.set_condition("campaign", False)
            raise RuntimeError("cannot submit an order without an owned Campaign")
        if intent.campaign_id not in {None, campaign_id}:
            self.gate.set_condition("campaign", False)
            raise RuntimeError("order intent Campaign does not match owned Campaign")
        intent = replace(intent, campaign_id=campaign_id)
        record = await self.executor.submit(
            intent,
            reference_price=intent.price,
        )
        if record.status == "SUBMIT_UNKNOWN":
            self.gate.set_condition("execution", False)
            self.risk_guard.halt("submit status unknown")
        if record.status == "REJECTED":
            response = record.payload.get("exchange_response") or {}
            code = response.get("code", "unknown")
            self.gate.set_condition("execution", False)
            self.risk_guard.halt(f"order rejected by exchange: {code}")
            raise RuntimeError(
                f"exchange rejected order {record.client_order_id}: {code}"
            )
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
        bucket_getter = getattr(self.strategy, "campaign_entry_bucket", None)
        entry_bucket = (
            bucket_getter(campaign_id) if callable(bucket_getter) else None
        )
        lease = CampaignLease(
            campaign_id,
            STRATEGY_ID,
            symbol,
            event_time,
            origin_price=None if origin_price is None else str(origin_price),
            entry_bucket=entry_bucket,
        )
        acquired = await self.campaign_store.acquire(lease)
        if acquired:
            self._owned_campaign_id = campaign_id
            self._owned_campaign_lease = lease
            self._queue_campaign_audit(
                "campaign_acquired",
                lease,
                event_time=event_time,
            )
        return acquired

    async def _persist_exit_state(self, symbol: str) -> None:
        async with self._campaign_lock:
            await self._persist_exit_state_locked(symbol)

    async def _persist_exit_state_locked(self, symbol: str) -> None:
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
        self._queue_campaign_audit(
            "campaign_exit_state_changed",
            self._owned_campaign_lease,
            event_time=self._now_ms(),
            details={
                "origin_checked": origin_checked,
                "reduced_at_origin": reduced_at_origin,
                "exit_requested": exit_requested,
            },
        )

    async def maybe_release_campaign(self, symbol: str) -> bool:
        async with self._campaign_lock:
            campaign_id = self._owned_campaign_id
            lease = self._owned_campaign_lease
            if campaign_id is None:
                return False
            if lease is None:
                if self.capital_store is not None:
                    return False
                lease = CampaignLease(
                    campaign_id,
                    STRATEGY_ID,
                    symbol,
                    self._now_ms(),
                )
            if lease.symbol != symbol:
                return False
            if self._submissions_inflight:
                return False
            if self.account.has_open_position(symbol):
                return False
            # An execution report can precede ACCOUNT_UPDATE. Keep the Campaign
            # lease until the position fact confirms the fill, even if the WAL is
            # already terminal.
            has_pending_position_update = getattr(
                self.account, "has_pending_position_update", None
            )
            if (
                has_pending_position_update is not None
                and has_pending_position_update(symbol)
            ):
                return False
            if not self.account.all_orders_terminal(symbol):
                return False
            await self._settle_campaign(lease)
            released = await self.campaign_store.release(campaign_id)
            if released:
                self._owned_campaign_id = None
                self._owned_campaign_lease = None
                if self._signal_arbiter.active_campaign_id == campaign_id:
                    self._signal_arbiter.release(campaign_id)
                self.gate.set_condition("campaign", True)
                self._queue_campaign_audit(
                    "campaign_released",
                    lease,
                    event_time=self._now_ms(),
                )
                await self._publish_audit()
            return released

    async def _settle_campaign(self, lease: CampaignLease) -> None:
        if self.capital_store is None:
            return
        if self.trade_source is None:
            raise RuntimeError("Campaign PnL source is unavailable")
        summary = await self.trade_source.get_campaign_pnl(
            account_id=self.account_id,
            strategy_id=STRATEGY_ID,
            campaign_id=lease.campaign_id,
        )
        # A Campaign whose entry never filled has no capital fact to settle.
        if summary is None:
            return
        self.gate.set_condition("capital", False)
        if (
            summary.campaign_id != lease.campaign_id
            or summary.symbol != lease.symbol
            or summary.has_open_quantity
            or summary.closed_at is None
        ):
            raise RuntimeError("Campaign PnL is not a complete closed fact")
        if self.funding_source is None:
            raise RuntimeError("Campaign funding source is unavailable")
        start_at = datetime.fromtimestamp(lease.started_at_ms / 1_000, tz=UTC)
        end_at = summary.closed_at + timedelta(milliseconds=1)
        funding = await self.funding_source.sync_funding_fee_total(
            account_id=self.account_id,
            symbol=lease.symbol,
            start_at=start_at,
            end_at=end_at,
        )
        result = await self.capital_store.settle(
            account_id=self.account_id,
            strategy_id=STRATEGY_ID,
            idempotency_key=lease.campaign_id,
            campaign_id=lease.campaign_id,
            net_pnl=summary.net_realized_pnl + funding,
            occurred_at=summary.closed_at,
        )
        snapshot = result.snapshot
        for strategy in self.strategy.strategies.values():
            strategy.total_notional = snapshot.state.trading_capital
        self.risk_guard.config.max_position_value_usdt = (
            snapshot.state.trading_capital
        )
        can_open = CapitalPolicy(snapshot.config).can_open(snapshot.state)
        if self.capital_admission_refresh is not None:
            can_open = bool(await self.capital_admission_refresh(snapshot)) and can_open
        self.gate.set_condition("capital", can_open)

    async def record_capital_admission(
        self,
        *,
        enabled: bool,
        wallet_capital: Decimal | None,
        required_capital: Decimal,
        reason: str,
    ) -> None:
        lease = self._owned_campaign_lease
        self._pending_audit_events += (
            StrategyAuditEvent(
                event_time=self._now_ms(),
                event_type="capital_admission_changed",
                symbol=(
                    lease.symbol
                    if lease is not None
                    else next(iter(self.strategy.strategies))
                ),
                strategy_id=STRATEGY_ID,
                campaign_id=None if lease is None else lease.campaign_id,
                details={
                    "enabled": enabled,
                    "wallet_capital": (
                        None if wallet_capital is None else str(wallet_capital)
                    ),
                    "required_capital": str(required_capital),
                    "reason": reason,
                },
            ),
        )
        await self._publish_audit()

    async def stop(self) -> None:
        await self.stop_execution_worker()
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

    async def _publish_audit(self) -> bool:
        if self.audit_sink is None:
            return True
        async with self._audit_lock:
            self._drain_strategy_audit_events()
            events = self._pending_audit_events
            if not events:
                return True
            try:
                await self.audit_sink(events)
            except asyncio.CancelledError:
                self._close_on_audit_failure("strategy audit write cancelled")
                raise
            except Exception as exc:
                self._close_on_audit_failure(
                    f"strategy audit write failed: {type(exc).__name__}"
                )
                return False
            self._pending_audit_events = self._pending_audit_events[len(events) :]
            return True

    async def _stage_strategy_audit_events(self) -> bool:
        if self.audit_sink is None:
            return False
        async with self._audit_lock:
            return self._drain_strategy_audit_events()

    def _drain_strategy_audit_events(self) -> bool:
        events = tuple(self.strategy.drain_audit_events())
        if events:
            self._pending_audit_events += events
        return bool(self._pending_audit_events)

    def _close_on_audit_failure(self, reason: str) -> None:
        self.gate.set_condition("execution", False)
        self.risk_guard.halt(reason)

    def _queue_campaign_audit(
        self,
        event_type: str,
        lease: CampaignLease,
        *,
        event_time: int,
        details: dict[str, object] | None = None,
    ) -> None:
        self._pending_audit_events += (
            StrategyAuditEvent(
                event_time=event_time,
                event_type=event_type,
                symbol=lease.symbol,
                strategy_id=lease.strategy_id,
                campaign_id=lease.campaign_id,
                details=details or {},
            ),
        )

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
            await self.coordinator.reconcile_exchange_symbol_admission()
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
