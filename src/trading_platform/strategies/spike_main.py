"""Spike testnet/live 正式运行进程入口。"""

from __future__ import annotations

import asyncio
import logging
import signal
import time
from contextlib import AsyncExitStack
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import httpx
import redis.asyncio as redis

from trading_platform.ledger.binance_runtime import create_binance_execution_runtime
from trading_platform.ledger.db.models import LedgerDB, create_connection_pool
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
from trading_platform.shared.risk import RiskConfig, RiskGuard
from trading_platform.strategies.admission import SubcategoryAdmissionService
from trading_platform.strategies.campaign_store import RedisCampaignStore
from trading_platform.strategies.spike_live import (
    ENTRY_REASONS,
    STRATEGY_ID,
    CompositeEntryGate,
    SpikeExecutionCoordinator,
    SpikeLiveSettings,
    SpikeRuntimeCallbacks,
    require_one_way_position_mode,
)
from trading_platform.strategies.spike_short import (
    DynamicSpikeBacktestStrategy,
    DynamicSpikeShortStrategy,
)
from trading_platform.strategies.universe import UNIVERSE_SCAN_INTERVAL_SECONDS


logger = logging.getLogger(__name__)


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
        self._stop = asyncio.Event()
        self._stack = AsyncExitStack()
        self._tasks: list[asyncio.Task] = []
        self._last_kline: dict[tuple[str, str], int] = {}
        self.http: httpx.AsyncClient | None = None
        self.redis: redis.Redis | None = None
        self.runtime = None
        self.coordinator: SpikeExecutionCoordinator | None = None
        self.admission: SubcategoryAdmissionService | None = None
        self.gate: CompositeEntryGate | None = None
        self.runtime_callbacks: SpikeRuntimeCallbacks | None = None

    async def start(self) -> None:
        if self.runtime is not None:
            return
        try:
            await self._build_resources()
            assert self.runtime is not None
            assert self.coordinator is not None
            assert self.gate is not None
            assert self.admission is not None

            await self.coordinator.restore_campaign_gate()
            await self.runtime.start()
            await self.coordinator.account.refresh_positions()
            await self.coordinator.reconcile_entry_expirations()
            self.coordinator.validate_recovered_campaign()
            await self.coordinator.restore_campaign_timing()
            if self.runtime_callbacks is not None:
                self.runtime_callbacks.finish_startup_recovery()
            for symbol in self.settings.symbols:
                await self.coordinator.maybe_release_campaign(symbol)
            self._restore_execution_gate()

            await self._register_market_subscriptions()
            await self._start_bar_consumer()
            await self._warm_strategy_history()
            await self._refresh_market_gate(require_ready=True)
            await self.admission.on_universe_scan()

            self._tasks.extend(
                [
                asyncio.create_task(self._kline_loop(), name="spike-kline-loop"),
                asyncio.create_task(self._safety_scan_loop(), name="spike-safety-scan"),
                ]
            )
        except BaseException:
            await self.stop()
            raise

    async def run(self) -> None:
        await self.start()
        task = asyncio.create_task(self._stop.wait(), name="spike-stop-wait")
        try:
            done, _ = await asyncio.wait(
                [task, *self._tasks], return_when=asyncio.FIRST_COMPLETED
            )
            for completed in done:
                if completed is not task:
                    completed.result()
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
        if self.runtime_callbacks is not None:
            self.runtime_callbacks.abort_startup_recovery()
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
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
        db = LedgerDB(pool)

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
        symbol_rules = BinanceSymbolRuleBook.from_exchange_info(
            await rest.get_exchange_info(), symbols=self.settings.symbols
        )
        for symbol in self.settings.symbols:
            require_viable_entry_notional(
                self.settings.total_notional,
                symbol_rules.get(symbol),
            )
        executor = BinanceOrderExecutor(
            rest,
            wal,
            account_id=self.settings.account_id,
            risk_guard=risk,
            symbol_rules=symbol_rules,
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
        )
        self.gate = CompositeEntryGate(strategy)
        for condition in ("execution", "market", "subcategory", "campaign"):
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

    def _restore_execution_gate(self) -> bool:
        if self.gate is None or self.coordinator is None or self.runtime is None:
            return False
        try:
            ready = (
                self.runtime.user_stream.connected
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
                "symbols": self.settings.symbols,
                "types": ["bar1s", "kline:1m", "kline:5m", "kline:15m"],
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

    async def _warm_strategy_history(self) -> None:
        assert self.coordinator is not None
        rest = self.coordinator.account.rest_client
        now_ms = int(time.time() * 1000)
        strategy = self.coordinator.strategy
        strategy.set_trading_enabled(False)
        for symbol in self.settings.symbols:
            for interval, limit, minimum in (
                ("1m", 1000, 960),
                ("5m", 100, 15),
                ("15m", 100, 10),
            ):
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

    async def _start_bar_consumer(self) -> None:
        """订阅 Redis 后才允许等待依赖消费者存在的市场质量门禁。"""
        ready = asyncio.Event()
        task = asyncio.create_task(self._bar_loop(ready), name="spike-bar-loop")
        self._tasks.append(task)
        waiter = asyncio.create_task(ready.wait(), name="spike-bar-ready")
        try:
            done, _ = await asyncio.wait(
                {task, waiter},
                timeout=10,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if waiter not in done:
                if task in done:
                    task.result()
                raise RuntimeError("bar consumer did not become ready")
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
                await self.coordinator.on_bar1s(Bar1s.from_json(message["data"]))
        except asyncio.CancelledError:
            raise
        except BaseException:
            assert self.gate is not None
            self.gate.set_condition("market", False)
            raise
        finally:
            await pubsub.aclose()

    async def _kline_loop(self) -> None:
        assert self.redis is not None
        assert self.coordinator is not None
        while True:
            for symbol in self.settings.symbols:
                for interval in ("1m", "5m", "15m"):
                    raw = await self.redis.hget(f"kline:{symbol}:{interval}", "latest")
                    if not raw:
                        continue
                    kline = Kline.from_json(raw)
                    key = (symbol, interval)
                    if kline.close_time <= self._last_kline.get(key, -1):
                        continue
                    await self.coordinator.on_kline(kline)
                    self._last_kline[key] = kline.close_time
            await asyncio.sleep(1)

    async def _safety_scan_loop(self) -> None:
        assert self.admission is not None
        assert self.coordinator is not None
        assert self.gate is not None
        while True:
            await asyncio.sleep(UNIVERSE_SCAN_INTERVAL_SECONDS)
            await self._register_market_subscriptions()
            await self._refresh_market_gate()
            await self.admission.on_universe_scan()
            await self.coordinator.restore_campaign_gate()
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
