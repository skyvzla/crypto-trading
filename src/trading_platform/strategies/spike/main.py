"""Spike testnet/live 正式运行进程入口。"""

from __future__ import annotations

import asyncio
import logging
import signal
import time
from contextlib import AsyncExitStack
from collections.abc import Sequence
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import httpx
import redis.asyncio as redis

from trading_platform.ledger.binance_runtime import create_binance_execution_runtime
from trading_platform.ledger.db.models import (
    LedgerDB,
    StrategyRuntimeStatus,
    create_connection_pool,
)
from trading_platform.shared.binance.live_executor import BinanceOrderExecutor
from trading_platform.shared.binance.rest_client import BinanceRestClient
from trading_platform.shared.binance.strategy_account import BinanceStrategyAccount
from trading_platform.shared.binance.symbol_rules import (
    BinanceSymbolRuleBook,
    BinanceSymbolRules,
)
from trading_platform.shared.config import (
    BinanceConfig,
    DatabaseConfig,
    RedisConfig,
    StrategyConfig,
)
from trading_platform.shared.events import Bar1s, Kline
from trading_platform.shared.execution_recovery import OrderWAL
from trading_platform.shared.logging_config import setup_logger
from trading_platform.shared.postgres_lease import PostgresExecutionLease
from trading_platform.shared.risk import RiskConfig, RiskGuard
from trading_platform.strategies.admission import SubcategoryAdmissionService
from trading_platform.strategies.campaign_store import RedisCampaignStore
from trading_platform.strategies.spike.live import (
    ENTRY_REASONS,
    STRATEGY_ID,
    CompositeEntryGate,
    SpikeExecutionCoordinator,
    SpikeLiveSettings,
    SpikeRuntimeCallbacks,
    require_one_way_position_mode,
)
from trading_platform.strategies.spike.short import (
    DynamicSpikeBacktestStrategy,
    DynamicSpikeShortStrategy,
)
from trading_platform.strategies.spike.definition import load_strategy_definition
from trading_platform.ledger.exchange_symbols import fetch_exchange_info_with_retry
from trading_platform.strategies.universe import (
    UNIVERSE_SCAN_INTERVAL_SECONDS,
    ExchangeSymbolSnapshot,
)


logger = logging.getLogger(__name__)
BAR_STREAM_STALE_SECONDS = 10.0
RUNTIME_HEARTBEAT_SECONDS = 5.0
EXCHANGE_RULE_REFRESH_INTERVAL_SECONDS = 24 * 60 * 60
MARKET_EVENT_QUEUE_MAXSIZE = 1_024


def _snapshot_from_database(
    managed_symbols: Sequence[str], allowed_symbols: Sequence[str]
) -> ExchangeSymbolSnapshot:
    managed = {symbol.strip().upper() for symbol in managed_symbols}
    database_allowed = {symbol.strip().upper() for symbol in allowed_symbols}
    allowed = managed & database_allowed
    blocked = managed - allowed
    return ExchangeSymbolSnapshot(
        allowed_symbols=frozenset(allowed),
        blocked_symbols=frozenset(blocked),
        blocked_reasons={symbol: "database_admission" for symbol in blocked},
    )


def require_viable_entry_notional(
    total_notional: Decimal,
    rules: BinanceSymbolRules,
) -> None:
    """拒绝任何确定会低于交易所最小名义金额的三档配置。"""
    smallest_tier = total_notional * min(DynamicSpikeShortStrategy.TIER_WEIGHTS)
    if smallest_tier <= rules.min_notional:
        raise ValueError(
            f"{rules.symbol} smallest entry tier must exceed min notional: "
            f"{smallest_tier} <= {rules.min_notional}"
        )


class SpikeLiveProcess:
    """拥有 Spike 进程全部资源，并按 fail-closed 顺序启动和关闭。"""

    CONTINUITY_STABLE_BARS = 60
    STREAM_RECOVERY_LIMIT = 900

    def __init__(
        self,
        settings: SpikeLiveSettings,
        *,
        binance: BinanceConfig,
        database: DatabaseConfig,
        redis_config: RedisConfig,
        strategy_config: StrategyConfig,
    ):
        if strategy_config.account_id != settings.account_id:
            raise ValueError("STRATEGY_ACCOUNT_ID must match SPIKE_ACCOUNT_ID")
        self.settings = settings
        self.binance = binance
        self.database = database
        self.redis_config = redis_config
        self.strategy_config = strategy_config
        self.strategy_definition = load_strategy_definition(settings.strategy_path)
        requirements = self.strategy_definition.data_requirements
        if requirements.metrics_5m:
            raise ValueError(
                "live Spike runtime does not provide metrics_5m yet: "
                f"{settings.strategy_path}"
            )
        if "1s" not in requirements.market_timeframes:
            raise ValueError(
                "live Spike runtime currently requires a 1s-driven strategy: "
                f"{settings.strategy_path}"
            )
        self._stop = asyncio.Event()
        self._stack = AsyncExitStack()
        self._tasks: list[asyncio.Task] = []
        self._market_events: asyncio.Queue[Bar1s | Kline] = asyncio.Queue(
            maxsize=MARKET_EVENT_QUEUE_MAXSIZE
        )
        self._market_event_queue_overflowed = False
        self._queued_execution_started = False
        self._last_kline: dict[tuple[str, str], int] = {}
        self._last_bar_received_monotonic: dict[str, float] = {}
        self._last_bar_trade_id: dict[str, int] = {}
        self._bar_continuity_streak: dict[str, int] = {}
        self._continuity_failed_symbols: set[str] = set()
        self._market_instance_epoch: str | None = None
        self.http: httpx.AsyncClient | None = None
        self.redis: redis.Redis | None = None
        self.runtime = None
        self.coordinator: SpikeExecutionCoordinator | None = None
        self.admission: SubcategoryAdmissionService | None = None
        self.gate: CompositeEntryGate | None = None
        self.runtime_callbacks: SpikeRuntimeCallbacks | None = None
        self.execution_lease: PostgresExecutionLease | None = None
        self.db: LedgerDB | None = None
        self.execution_rest: BinanceRestClient | None = None
        self.exchange_symbol_snapshot: ExchangeSymbolSnapshot | None = None
        self._exchange_rules_synced_monotonic: float | None = None
        self.instance_id = uuid4().hex
        self.started_at = datetime.now(timezone.utc)
        self._runtime_fatal_reason: str | None = None

    async def start(self) -> None:
        if self.runtime is not None:
            return
        try:
            await self._build_resources()
            assert self.runtime is not None
            assert self.coordinator is not None
            assert self.gate is not None
            assert self.admission is not None

            if self.execution_lease is not None:
                self._tasks.append(
                    asyncio.create_task(
                        self._execution_lease_fatal_loop(),
                        name="spike-execution-lease-fatal",
                    )
                )

            await self.coordinator.restore_campaign_gate()
            await self.runtime.start()
            await self.coordinator.account.refresh_positions()
            await self._refresh_exchange_symbol_admission()
            await self.coordinator.reconcile_entry_expirations()
            self.coordinator.validate_recovered_campaign()
            await self.coordinator.restore_campaign_timing()
            if self.runtime_callbacks is not None:
                self.runtime_callbacks.finish_startup_recovery()
            for symbol in self.settings.symbols:
                await self.coordinator.maybe_release_campaign(symbol)
            self._restore_execution_gate()

            execution_worker = self.coordinator.start_execution_worker()
            self._tasks.append(execution_worker)
            self._queued_execution_started = True
            self.gate.set_condition("event_queue", True)

            await self._register_market_subscriptions()
            await self._start_bar_consumer()
            await self._warm_strategy_history()
            await self._refresh_market_gate(require_ready=True)
            await self.admission.on_universe_scan()
            await self.coordinator._flush_cancellations()

            self._tasks.extend(
                [
                asyncio.create_task(self._kline_loop(), name="spike-kline-loop"),
                asyncio.create_task(
                    self._market_watchdog_loop(), name="spike-market-watchdog"
                ),
                asyncio.create_task(self._safety_scan_loop(), name="spike-safety-scan"),
                asyncio.create_task(
                    self._execution_stream_fatal_loop(),
                    name="spike-execution-stream-fatal",
                ),
                asyncio.create_task(
                    self._submit_unknown_fatal_loop(),
                    name="spike-submit-unknown-fatal",
                ),
                ]
            )
            await self._publish_runtime_status()
            self._tasks.append(
                asyncio.create_task(
                    self._runtime_heartbeat_loop(),
                    name="spike-runtime-heartbeat",
                )
            )
        except BaseException as exc:
            self._runtime_fatal_reason = f"startup failed: {type(exc).__name__}"
            await self.stop()
            raise

    async def run(self) -> None:
        await self.start()
        task = asyncio.create_task(self._stop.wait(), name="spike-stop-wait")
        try:
            done, _ = await asyncio.wait(
                [task, *self._tasks], return_when=asyncio.FIRST_COMPLETED
            )
            background_done = done - {task}
            for completed in background_done:
                task_name = completed.get_name()
                try:
                    completed.result()
                except BaseException as exc:
                    if self._runtime_fatal_reason is None:
                        self._mark_runtime_fatal(
                            f"background task failed: {task_name}: "
                            f"{type(exc).__name__}"
                        )
                        await self._try_publish_fatal_status()
                    raise
                reason = f"background task exited unexpectedly: {task_name}"
                self._mark_runtime_fatal(reason)
                await self._try_publish_fatal_status()
                raise RuntimeError(reason)
            if task in done:
                return
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await self.stop()

    def request_stop(self) -> None:
        self._stop.set()

    async def stop(self) -> None:
        if self.gate is not None:
            self.gate.set_condition("execution", False)
            self.gate.set_condition("market", False)
            self.gate.set_condition("event_queue", False)
        if self.runtime_callbacks is not None:
            self.runtime_callbacks.abort_startup_recovery()
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        self._queued_execution_started = False
        errors: list[BaseException] = []
        if self.coordinator is not None:
            try:
                await self.coordinator.stop()
            except BaseException as exc:
                errors.append(exc)
        if self.runtime is not None:
            try:
                await self.runtime.stop()
            except BaseException as exc:
                errors.append(exc)
            self.runtime = None
        if self.db is not None:
            try:
                await self._publish_runtime_status(
                    status="fatal" if self._runtime_fatal_reason else "stopped",
                    stopped=True,
                )
            except BaseException as exc:
                errors.append(exc)
        try:
            await self._unregister_market_subscriptions()
        except BaseException as exc:
            errors.append(exc)
        try:
            await self._stack.aclose()
        except BaseException as exc:
            errors.append(exc)
        if errors:
            raise BaseExceptionGroup("Spike shutdown failed", errors)

    async def _build_resources(self) -> None:
        pool = await create_connection_pool(self.database.dsn)
        self._stack.push_async_callback(pool.close)
        self.execution_lease = PostgresExecutionLease(pool, self.settings.account_id)
        await self.execution_lease.acquire()
        self._stack.push_async_callback(self.execution_lease.release)
        db = LedgerDB(pool)
        self.db = db

        self.redis = redis.Redis(
            host=self.redis_config.host,
            port=self.redis_config.port,
            db=self.redis_config.db,
            password=self.redis_config.password,
            decode_responses=True,
        )
        await self.redis.ping()
        self._stack.push_async_callback(self.redis.aclose)

        self.http = httpx.AsyncClient(
            base_url=self.strategy_config.market_api_url, timeout=10
        )
        self._stack.push_async_callback(self.http.aclose)

        rest = BinanceRestClient(
            self.binance.api_key,
            self.binance.api_secret,
            base_url=self.binance.base_url,
        )
        self.execution_rest = rest
        self._stack.push_async_callback(rest.close)
        require_one_way_position_mode(await rest.get_position_mode())
        risk = RiskGuard(
            self.settings.account_id,
            RiskConfig(
                max_position_value_usdt=Decimal(
                    str(self.strategy_config.risk_max_position_value_usdt)
                ),
                max_symbols=self.strategy_config.risk_max_symbols,
            ),
        )
        if self.settings.total_notional > risk.config.max_position_value_usdt:
            raise ValueError("total_notional exceeds process risk limit")
        wal = OrderWAL(self.settings.wal_path)
        initial_symbol_snapshot = _snapshot_from_database(
            self.settings.symbols,
            await db.list_tradeable_exchange_symbols(
                freeze_days=self.settings.delisting_freeze_days,
                strategy_id=STRATEGY_ID,
            ),
        )
        execution_exchange_info = await fetch_exchange_info_with_retry(
            rest.get_exchange_info,
            on_retry=lambda attempt, total, error: logger.warning(
                "initial execution exchangeInfo retry %s/%s: %s: %s",
                attempt,
                total,
                type(error).__name__,
                error,
            ),
        )
        symbol_rules = self._build_symbol_rule_book(execution_exchange_info)
        self._exchange_rules_synced_monotonic = asyncio.get_running_loop().time()
        for symbol in initial_symbol_snapshot.allowed_symbols:
            require_viable_entry_notional(
                self.settings.total_notional,
                symbol_rules.get(symbol),
            )
        account = BinanceStrategyAccount(
            rest,
            wal,
            account_id=self.settings.account_id,
            strategy_id=STRATEGY_ID,
            risk_guard=risk,
        )
        strategy = DynamicSpikeBacktestStrategy(
            self.settings.symbols,
            self.settings.total_notional,
            account=account,
            exit_policy=self.settings.exit_policy,
            prior_high_lookback_minutes=(
                self.strategy_definition.defaults.prior_high_lookback_hours * 60
            ),
            entry_tier_mode=self.strategy_definition.defaults.entry_tier_mode,
            rise_low_lookback_minutes=(
                self.strategy_definition.defaults.rise_low_lookback_hours * 60
            ),
            min_rise_duration_minutes=(
                self.strategy_definition.defaults.min_rise_duration_hours * 60
            ),
            early_profit_unlock_ratio=(
                Decimal(str(self.strategy_definition.defaults.profit_unlock_percent))
                / Decimal("100")
                if self.strategy_definition.defaults.profit_unlock_percent is not None
                else None
            ),
            strategy_class=self.strategy_definition.strategy_class,
        )
        executor = BinanceOrderExecutor(
            rest,
            wal,
            account_id=self.settings.account_id,
            risk_guard=risk,
            symbol_rules=symbol_rules,
            can_open_symbol=strategy.is_symbol_entry_enabled,
        )
        strategy.set_trading_enabled(False)
        self.gate = CompositeEntryGate(strategy)
        for condition in (
            "execution",
            "market",
            "bar_stream",
            "bar_continuity",
            "subcategory",
            "campaign",
            "exchange_symbols",
            "event_queue",
        ):
            self.gate.set_condition(condition, False)
        self.coordinator = SpikeExecutionCoordinator(
            strategy=strategy,
            account=account,
            executor=executor,
            campaign_store=RedisCampaignStore(self.redis),
            risk_guard=risk,
            gate=self.gate,
            account_id=self.settings.account_id,
            trade_source=db,
            audit_sink=lambda events: db.insert_strategy_audit_events(
                events, account_id=self.settings.account_id
            ),
        )
        self.admission = SubcategoryAdmissionService(
            source=db,
            gate=self.gate.admission_view(),
            account=account,
            subcategory=self.settings.subcategory,
            strategy_id=STRATEGY_ID,
            entry_trigger_reasons=ENTRY_REASONS,
        )
        self.runtime = create_binance_execution_runtime(
            rest_client=rest,
            executor=executor,
            db=db,
            account_id=self.settings.account_id,
            strategy_id=STRATEGY_ID,
            managed_symbols=self.settings.symbols,
            dedicated_strategy_account=self.settings.dedicated_strategy_account,
            ws_base_url=self.binance.ws_base_url,
            poll_interval_seconds=self.settings.poll_interval_seconds,
            max_poll_attempts=self.settings.max_poll_attempts,
        )
        stream = self.runtime.user_stream
        ledger_callbacks = SimpleNamespace(
            handle_execution_report=stream.on_execution_report,
            handle_account_update=stream.on_account_update,
        )
        callbacks = SpikeRuntimeCallbacks(
            delegate=ledger_callbacks,
            account=account,
            coordinator=self.coordinator,
            gate=self.gate,
            risk_guard=risk,
        )
        callbacks.begin_startup_recovery()
        self.runtime_callbacks = callbacks
        self.runtime.user_stream.on_execution_report = callbacks.handle_execution_report
        self.runtime.user_stream.on_account_update = callbacks.handle_account_update
        self.runtime.user_stream.on_disconnect = self._on_execution_stream_disconnected
        self.runtime.on_recovered = self._restore_execution_gate

    def _on_execution_stream_disconnected(self) -> None:
        if self.gate is not None:
            self.gate.set_condition("execution", False)

    async def _execution_stream_fatal_loop(self) -> None:
        assert self.runtime is not None
        exc = await self.runtime.user_stream.wait_fatal()
        reason = f"execution stream callback failed: {type(exc).__name__}"
        self._mark_runtime_fatal(reason)
        await self._try_publish_fatal_status()
        raise RuntimeError("execution stream callback failed") from exc

    async def _execution_lease_fatal_loop(self) -> None:
        assert self.execution_lease is not None
        exc = await self.execution_lease.wait_lost()
        reason = f"execution account lease lost: {type(exc).__name__}"
        self._mark_runtime_fatal(reason)
        await self._try_publish_fatal_status()
        raise RuntimeError("execution account lease lost") from exc

    async def _submit_unknown_fatal_loop(self) -> None:
        assert self.runtime is not None
        exc = await self.runtime.unknown_poller.wait_fatal()
        self._mark_runtime_fatal("SUBMIT_UNKNOWN resolution attempts exhausted")
        await self._try_publish_fatal_status()
        raise RuntimeError("SUBMIT_UNKNOWN recovery failed") from exc

    async def _runtime_heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(RUNTIME_HEARTBEAT_SECONDS)
            try:
                await self._publish_runtime_status()
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                reason = f"runtime status heartbeat failed: {type(exc).__name__}"
                self._mark_runtime_fatal(reason)
                raise RuntimeError(reason) from exc

    def _mark_runtime_fatal(self, reason: str) -> None:
        self._runtime_fatal_reason = reason
        if self.gate is not None:
            self.gate.set_condition("execution", False)
        if self.coordinator is not None:
            self.coordinator.risk_guard.halt(reason)

    async def _try_publish_fatal_status(self) -> None:
        try:
            await self._publish_runtime_status(status="fatal")
        except BaseException:
            logger.exception("Failed to publish Spike fatal runtime status")

    async def _publish_runtime_status(
        self,
        *,
        status: str | None = None,
        stopped: bool = False,
    ) -> None:
        if self.db is None:
            return
        now = datetime.now(timezone.utc)
        gates = {} if self.gate is None else self.gate.snapshot()
        halted = bool(
            self.coordinator is not None and self.coordinator.risk_guard.halted
        )
        halt_reason = (
            self._runtime_fatal_reason
            or (
                self.coordinator.risk_guard.halt_reason
                if self.coordinator is not None
                else None
            )
            or None
        )
        if status is None:
            safety_ready = all(
                gates.get(name, False)
                for name in ("execution", "market", "bar_stream")
            ) and gates.get("event_queue", True)
            status = "running" if safety_ready and not halted else "degraded"
        accepted = await self.db.upsert_strategy_runtime_status(
            StrategyRuntimeStatus(
                account_id=self.settings.account_id,
                strategy_id=STRATEGY_ID,
                instance_id=self.instance_id,
                mode=self.settings.mode,
                status=status,
                entry_enabled=bool(self.gate and self.gate.enabled),
                halted=halted,
                halt_reason=halt_reason,
                gate_conditions=gates,
                started_at=self.started_at,
                heartbeat_at=now,
                stopped_at=now if stopped else None,
            )
        )
        if not accepted:
            if self.gate is not None:
                self.gate.set_condition("execution", False)
            if self.coordinator is not None:
                self.coordinator.risk_guard.halt(
                    "runtime status ownership lost to another instance"
                )
            raise RuntimeError("runtime status ownership lost to another instance")

    def _restore_execution_gate(self) -> bool:
        if self.gate is None or self.coordinator is None or self.runtime is None:
            return False
        try:
            ready = (
                (self.execution_lease is None or self.execution_lease.held)
                and self.runtime.user_stream.connected
                and not self.coordinator.risk_guard.halted
                and not self.coordinator.account.has_unresolved_orders()
            )
        except Exception:
            self.coordinator.risk_guard.halt("execution readiness check failed")
            ready = False
        self.gate.set_condition("execution", ready)
        return ready

    async def _register_market_subscriptions(self) -> None:
        assert self.http is not None
        response = await self.http.put(
            f"/subscriptions/{self._consumer_id}",
            json={
                "symbols": list(self._market_symbols()),
                "types": self._market_subscription_types(),
            },
        )
        response.raise_for_status()

    async def _unregister_market_subscriptions(self) -> None:
        if self.http is None:
            return
        try:
            await self.http.delete(f"/subscriptions/{self._consumer_id}")
        except Exception:
            logger.exception("failed to unregister market subscription")

    def _market_subscription_types(self) -> list[str]:
        timeframes = list(self.strategy_definition.data_requirements.market_timeframes)
        if self.settings.exit_policy == "candidate-v1" and "15m" not in timeframes:
            timeframes.append("15m")
        return [
            "bar1s" if timeframe == "1s" else f"kline:{timeframe}"
            for timeframe in timeframes
        ]

    async def _warm_strategy_history(self) -> None:
        assert self.coordinator is not None
        rest = self.coordinator.account.rest_client
        now_ms = int(time.time() * 1000)
        strategy = self.coordinator.strategy
        strategy.set_trading_enabled(False)
        for symbol in self._market_symbols():
            warmup_requirements = {
                "1m": (1000, 960),
                "5m": (100, 15),
                "15m": (100, 10),
            }
            for interval in self._required_kline_intervals():
                if interval not in warmup_requirements:
                    raise RuntimeError(
                        f"missing live warmup policy for {interval}"
                    )
                limit, minimum = warmup_requirements[interval]
                rows = await rest.get_klines(
                    symbol, interval, limit=limit, end_time=now_ms
                )
                klines = [
                    self._parse_binance_kline(symbol, interval, row)
                    for row in rows
                ]
                completed = [
                    kline for kline in klines if kline.close_time < now_ms
                ]
                if len(completed) < minimum:
                    raise RuntimeError(
                        f"insufficient {interval} warmup for {symbol}: "
                        f"{len(completed)} < {minimum}"
                    )
                for kline in completed:
                    await self.coordinator.on_kline(kline)
                self._last_kline[(symbol, interval)] = completed[-1].close_time
        strategy.refresh_candidate_features()
        strategy.set_trading_enabled(True)

    async def _refresh_market_gate(self, *, require_ready: bool = False) -> bool:
        assert self.http is not None
        assert self.gate is not None
        attempts = 30 if require_ready else 1
        for attempt in range(attempts):
            try:
                health = (await self.http.get("/health")).json()
                epoch = health.get("instance_epoch")
                if not isinstance(epoch, str) or not epoch:
                    raise RuntimeError("market health has no instance_epoch")
                if self._market_instance_epoch is None:
                    self._market_instance_epoch = epoch
                elif epoch != self._market_instance_epoch:
                    await self._handle_market_epoch_change(epoch)
                quality_response = await self.http.get("/quality")
                quality_response.raise_for_status()
                quality = quality_response.json()
                expected_testnet = self.settings.mode == "testnet"
                ready = (
                    health.get("status") == "ready"
                    and health.get("binance_testnet") is expected_testnet
                    and quality.get("ready") is True
                )
            except Exception:
                ready = False
            self.gate.set_condition("market", ready)
            if ready or not require_ready:
                return ready
            if attempt + 1 < attempts:
                await asyncio.sleep(2)
        raise RuntimeError("market layer is not ready or uses a different environment")

    async def _handle_market_epoch_change(self, epoch: str) -> None:
        """Market 重启会丢失内存订阅，立即重注册并重建消费水位。"""
        assert self.gate is not None
        assert self.coordinator is not None
        previous = self._market_instance_epoch
        self._last_bar_trade_id.clear()
        self._bar_continuity_streak.clear()
        self._last_bar_received_monotonic.clear()
        self._continuity_failed_symbols = set(self._market_symbols())
        self.gate.set_condition("bar_continuity", False)
        self.gate.set_condition("bar_stream", False)
        await self.coordinator.cancel_open_entry_orders()
        await self._register_market_subscriptions()
        self._market_instance_epoch = epoch
        logger.warning("Market instance changed: %s -> %s", previous, epoch)

    async def _start_bar_consumer(self) -> None:
        """订阅 Redis 后才允许等待依赖消费者存在的市场质量门禁。"""
        ready = asyncio.Event()
        strategy_task = asyncio.create_task(
            self._strategy_event_loop(), name="spike-strategy-event-loop"
        )
        self._tasks.append(strategy_task)
        task = asyncio.create_task(self._bar_loop(ready), name="spike-bar-loop")
        self._tasks.append(task)
        waiter = asyncio.create_task(ready.wait(), name="spike-bar-ready")
        try:
            done, _ = await asyncio.wait(
                {strategy_task, task, waiter},
                timeout=10,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if waiter not in done:
                if strategy_task in done:
                    strategy_task.result()
                if task in done:
                    task.result()
                raise RuntimeError("bar consumer did not become ready")
            if strategy_task.done():
                strategy_task.result()
                raise RuntimeError("strategy event loop stopped during startup")
            if task.done():
                task.result()
                raise RuntimeError("bar consumer stopped during startup")
        finally:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)

    async def _bar_loop(self, ready: asyncio.Event | None = None) -> None:
        assert self.redis is not None
        assert self.coordinator is not None
        pubsub = self.redis.pubsub()
        try:
            await pubsub.subscribe(*(f"bar1s:{symbol}" for symbol in self.settings.symbols))
            if ready is not None:
                ready.set()
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                bar = Bar1s.from_json(message["data"])
                if bar.symbol not in self.settings.symbols:
                    raise RuntimeError(
                        f"unexpected bar symbol on managed subscription: {bar.symbol}"
                    )
                await self._enqueue_market_event(bar)
        except asyncio.CancelledError:
            raise
        except BaseException:
            assert self.gate is not None
            self.gate.set_condition("market", False)
            raise
        finally:
            await pubsub.aclose()

    async def _enqueue_market_event(self, event: Bar1s | Kline) -> None:
        """正常路径不等待策略；满载时关闭入场并背压，保证事件不丢。"""

        try:
            self._market_events.put_nowait(event)
            return
        except asyncio.QueueFull:
            pass
        if not self._market_event_queue_overflowed:
            self._market_event_queue_overflowed = True
            assert self.gate is not None
            assert self.coordinator is not None
            self.gate.set_condition("event_queue", False)
            self.coordinator.close_entry_pipeline(
                "market event queue full",
                symbol=event.symbol,
                event_time=event.available_time,
            )
            logger.error(
                "%s 行情事件队列已满，关闭入场并保留背压事件",
                event.symbol,
            )
        await self._market_events.put(event)

    async def _strategy_event_loop(self) -> None:
        """按实际入队顺序串行更新策略状态，网络执行由独立 worker 处理。"""

        while True:
            event = await self._market_events.get()
            try:
                if isinstance(event, Bar1s):
                    await self._handle_live_bar(event)
                else:
                    assert self.coordinator is not None
                    if self._queued_execution_started:
                        await self.coordinator.on_kline_queued(event)
                    else:
                        await self.coordinator.on_kline(event)
            finally:
                self._market_events.task_done()

    async def _deliver_bar1s(self, bar: Bar1s) -> None:
        assert self.coordinator is not None
        if self._queued_execution_started:
            await self.coordinator.on_bar1s_queued(bar)
        else:
            await self.coordinator.on_bar1s(bar)

    async def _handle_live_bar(self, bar: Bar1s) -> None:
        """校验逐币种水位，必要时回放缺口，然后交给策略串行处理。"""
        assert self.coordinator is not None
        self._last_bar_received_monotonic[bar.symbol] = (
            asyncio.get_running_loop().time()
        )
        self._refresh_bar_stream_gate()

        first_id = bar.first_aggregate_trade_id
        last_id = bar.last_aggregate_trade_id
        previous_id = self._last_bar_trade_id.get(bar.symbol)
        if first_id is None or last_id is None or first_id > last_id:
            await self._fail_bar_continuity(bar.symbol, "missing or invalid watermark")
            self._last_bar_trade_id.pop(bar.symbol, None)
            self._bar_continuity_streak[bar.symbol] = 0
            await self._deliver_bar1s(bar)
            return

        if previous_id is not None and last_id <= previous_id:
            return

        if previous_id is None:
            await self._fail_bar_continuity(bar.symbol, "establishing initial watermark")
            self._bar_continuity_streak[bar.symbol] = 0

        if previous_id is not None and first_id != previous_id + 1:
            await self._fail_bar_continuity(
                bar.symbol,
                f"aggTrade gap {previous_id + 1}..{first_id - 1}",
            )
            recovered = []
            if first_id > previous_id + 1:
                recovered = await self._recover_bar_gap(
                    bar.symbol, previous_id + 1, first_id - 1
                )
            for recovered_bar in recovered:
                await self._deliver_bar1s(recovered_bar)
            self._bar_continuity_streak[bar.symbol] = 0

        self._last_bar_trade_id[bar.symbol] = last_id
        self._bar_continuity_streak[bar.symbol] = (
            self._bar_continuity_streak.get(bar.symbol, 0) + 1
        )
        await self._deliver_bar1s(bar)
        self._refresh_bar_continuity_gate()

    async def _fail_bar_continuity(self, symbol: str, reason: str) -> None:
        assert self.gate is not None
        assert self.coordinator is not None
        self.gate.set_condition("bar_continuity", False)
        if symbol not in self._continuity_failed_symbols:
            self._continuity_failed_symbols.add(symbol)
            await self.coordinator.cancel_open_entry_orders()
        logger.warning("%s 关闭开仓行情门禁: %s", symbol, reason)

    async def _recover_bar_gap(
        self, symbol: str, from_id: int, to_id: int
    ) -> list[Bar1s]:
        assert self.redis is not None
        assert self.http is not None
        try:
            rows = await self.redis.xrevrange(
                f"bar1s:stream:{symbol}",
                max="+",
                min="-",
                count=self.STREAM_RECOVERY_LIMIT,
            )
            stream_bars = []
            for _, fields in rows:
                raw = fields.get("data") or fields.get(b"data")
                if raw is not None:
                    stream_bars.append(Bar1s.from_json(raw))
            recovered = self._closed_bar_range(stream_bars, from_id, to_id)
            if recovered:
                return recovered

            response = await self.http.get(
                f"/bar1s/{symbol}/recover",
                params={"from_id": from_id, "to_id": to_id},
            )
            response.raise_for_status()
            api_bars = [Bar1s.from_dict(item) for item in response.json()["bars"]]
            recovered = self._closed_bar_range(api_bars, from_id, to_id)
            if not recovered:
                raise RuntimeError("recovery response does not close the gap")
            return recovered
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(
                "%s 行情缺口无法恢复，保持开仓关闭: %s: %s",
                symbol,
                type(exc).__name__,
                exc,
            )
            return []

    @staticmethod
    def _closed_bar_range(
        bars: list[Bar1s], from_id: int, to_id: int
    ) -> list[Bar1s]:
        candidates = sorted(
            (
                bar
                for bar in bars
                if bar.first_aggregate_trade_id is not None
                and bar.last_aggregate_trade_id is not None
                and bar.last_aggregate_trade_id >= from_id
                and bar.first_aggregate_trade_id <= to_id
            ),
            key=lambda item: (item.first_aggregate_trade_id or -1, item.timestamp),
        )
        recovered: list[Bar1s] = []
        cursor = from_id
        for candidate in candidates:
            first_id = candidate.first_aggregate_trade_id
            last_id = candidate.last_aggregate_trade_id
            assert first_id is not None and last_id is not None
            if last_id < cursor:
                continue
            if first_id != cursor or last_id > to_id:
                return []
            recovered.append(candidate)
            cursor = last_id + 1
            if cursor == to_id + 1:
                return recovered
        return []

    def _refresh_bar_continuity_gate(self) -> bool:
        assert self.gate is not None
        symbols = self._market_symbols()
        ready = bool(symbols) and all(
            self._bar_continuity_streak.get(symbol, 0)
            >= self.CONTINUITY_STABLE_BARS
            for symbol in symbols
        )
        if ready:
            self._continuity_failed_symbols.difference_update(symbols)
        self.gate.set_condition("bar_continuity", ready)
        return ready

    async def _kline_loop(self) -> None:
        assert self.redis is not None
        assert self.coordinator is not None
        while True:
            for symbol in self._market_symbols():
                for interval in self._required_kline_intervals():
                    raw = await self.redis.hget(f"kline:{symbol}:{interval}", "latest")
                    if not raw:
                        continue
                    kline = Kline.from_json(raw)
                    key = (symbol, interval)
                    if kline.close_time <= self._last_kline.get(key, -1):
                        continue
                    await self._enqueue_market_event(kline)
                    self._last_kline[key] = kline.close_time
            await asyncio.sleep(1)

    def _required_kline_intervals(self) -> tuple[str, ...]:
        intervals = [
            timeframe
            for timeframe in self.strategy_definition.data_requirements.market_timeframes
            if timeframe != "1s"
        ]
        if self.settings.exit_policy == "candidate-v1" and "15m" not in intervals:
            intervals.append("15m")
        return tuple(intervals)

    async def _safety_scan_loop(self) -> None:
        assert self.admission is not None
        assert self.coordinator is not None
        assert self.gate is not None
        while True:
            await asyncio.sleep(UNIVERSE_SCAN_INTERVAL_SECONDS)
            await self._refresh_exchange_symbol_admission()
            await self._register_market_subscriptions()
            await self._refresh_market_gate()
            await self.admission.on_universe_scan()
            await self.coordinator._flush_cancellations()
            await self.coordinator.restore_campaign_gate()
            self.coordinator.validate_recovered_campaign()
            await self.coordinator.reconcile_entry_expirations()
            if self.runtime is not None and self.runtime.is_running:
                if self.coordinator.account.has_unresolved_orders():
                    self.gate.set_condition("execution", False)
                    continue
                reconciler = self.runtime.startup_reconciler
                if reconciler is not None:
                    try:
                        await reconciler.reconcile_once()
                    except Exception:
                        self.gate.set_condition("execution", False)
                    else:
                        self._restore_execution_gate()

    async def _refresh_exchange_symbol_admission(self) -> bool:
        assert self.coordinator is not None
        assert self.gate is not None
        assert self.execution_rest is not None
        assert self.db is not None
        now_monotonic = asyncio.get_running_loop().time()
        rules_refresh_due = self._exchange_rules_synced_monotonic is None or (
            now_monotonic - self._exchange_rules_synced_monotonic
            >= EXCHANGE_RULE_REFRESH_INTERVAL_SECONDS
        )
        try:
            updated_symbol_rules: BinanceSymbolRuleBook | None = None
            if rules_refresh_due:
                execution_exchange_info = await fetch_exchange_info_with_retry(
                    self.execution_rest.get_exchange_info,
                    on_retry=lambda attempt, total, error: logger.warning(
                        "execution exchangeInfo retry %s/%s: %s: %s",
                        attempt,
                        total,
                        type(error).__name__,
                        error,
                    ),
                )
                updated_symbol_rules = self._build_symbol_rule_book(
                    execution_exchange_info
                )
                self._exchange_rules_synced_monotonic = now_monotonic
            snapshot = _snapshot_from_database(
                self.settings.symbols,
                await self.db.list_tradeable_exchange_symbols(
                    freeze_days=self.settings.delisting_freeze_days,
                    strategy_id=STRATEGY_ID,
                ),
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await self.coordinator.update_exchange_symbol_admission(frozenset())
            self.gate.set_condition("exchange_symbols", False)
            logger.error(
                "exchange symbol admission refresh failed closed: %s: %s",
                type(error).__name__,
                error,
            )
            return False

        blocked = await self.coordinator.update_exchange_symbol_admission(
            snapshot.allowed_symbols,
            symbol_rules=updated_symbol_rules,
        )
        self.exchange_symbol_snapshot = snapshot
        self.gate.set_condition("exchange_symbols", True)
        if blocked:
            logger.warning(
                "exchange symbol entry blocked: %s",
                ", ".join(
                    f"{symbol}={snapshot.blocked_reasons[symbol]}"
                    for symbol in sorted(blocked)
                ),
            )
        return True

    async def _market_watchdog_loop(self) -> None:
        """缩短市场质量失效到关闭准入之间的时间。"""
        while True:
            await asyncio.sleep(5)
            await self._refresh_market_safety_once()

    async def _refresh_market_safety_once(self) -> bool:
        """关闭不可靠行情准入，并撤销仍可能成交的入场挂单。"""
        assert self.coordinator is not None
        market_ready = await self._refresh_market_gate()
        stream_ready = self._refresh_bar_stream_gate()
        ready = market_ready and stream_ready
        if not ready:
            await self.coordinator.cancel_open_entry_orders()
        return ready

    def _refresh_bar_stream_gate(self, *, now: float | None = None) -> bool:
        """检测本进程 Redis 交付是否静默，不依赖上游健康声明。"""
        assert self.gate is not None
        current = asyncio.get_running_loop().time() if now is None else now
        ready = all(
            current - self._last_bar_received_monotonic.get(symbol, float("-inf"))
            <= BAR_STREAM_STALE_SECONDS
            for symbol in self._market_symbols()
        ) and not self._market_event_queue_overflowed
        self.gate.set_condition("bar_stream", ready)
        return ready

    def _market_symbols(self) -> tuple[str, ...]:
        allowed = (
            frozenset()
            if self.exchange_symbol_snapshot is None
            else self.exchange_symbol_snapshot.allowed_symbols
        )
        try:
            live_risk = (
                frozenset()
                if self.coordinator is None
                else frozenset(
                    self.coordinator.account.symbols_with_live_risk()
                )
            )
        except Exception:
            live_risk = frozenset(self.settings.symbols)
        managed = frozenset(self.settings.symbols)
        return tuple(sorted((allowed | live_risk) & managed))

    def _build_symbol_rule_book(
        self, exchange_info: dict[str, Any]
    ) -> BinanceSymbolRuleBook:
        managed = frozenset(self.settings.symbols)
        rule_symbols = [
            str(item.get("symbol"))
            for item in exchange_info.get("symbols", [])
            if isinstance(item, dict)
            and item.get("contractType") == "PERPETUAL"
            and item.get("symbol") in managed
        ]
        if not rule_symbols:
            return BinanceSymbolRuleBook({})
        return BinanceSymbolRuleBook.from_exchange_info(
            exchange_info,
            symbols=rule_symbols,
            require_trading=False,
        )

    @property
    def _consumer_id(self) -> str:
        return f"spike_{self.settings.account_id}"

    @staticmethod
    def _parse_binance_kline(symbol: str, interval: str, row: list[Any]) -> Kline:
        if len(row) < 7:
            raise RuntimeError("invalid Binance kline row")
        close_time = int(row[6])
        return Kline(
            symbol=symbol,
            interval=interval,
            open_time=int(row[0]),
            close_time=close_time,
            available_time=close_time + 1,
            open=Decimal(str(row[1])),
            high=Decimal(str(row[2])),
            low=Decimal(str(row[3])),
            close=Decimal(str(row[4])),
            volume=Decimal(str(row[5])),
        )


async def async_main() -> None:
    settings = SpikeLiveSettings()
    if settings.mode == "testnet":
        binance = BinanceConfig(
            testnet=True,
            base_url="https://demo-fapi.binance.com",
            ws_base_url="wss://stream.binancefuture.com",
        )
    else:
        binance = BinanceConfig(testnet=False)
    if not binance.api_key or not binance.api_secret:
        raise ValueError("Binance API credentials are required")
    strategy_config = StrategyConfig(account_id=settings.account_id)
    process = SpikeLiveProcess(
        settings,
        binance=binance,
        database=DatabaseConfig(),
        redis_config=RedisConfig(),
        strategy_config=strategy_config,
    )
    loop = asyncio.get_running_loop()
    for name in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(name, process.request_stop)
    await process.run()


def main() -> None:
    setup_logger("trading_platform")
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
