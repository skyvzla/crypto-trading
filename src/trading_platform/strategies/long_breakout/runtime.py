"""Dedicated testnet runtime for the long 7-day box breakout strategy."""

from __future__ import annotations

import asyncio
import signal
from contextlib import AsyncExitStack
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace
from typing import Any, Awaitable
from uuid import uuid4

import httpx

from trading_platform.ledger.binance_runtime import create_binance_execution_runtime
from trading_platform.ledger.db.models import (
    LedgerDB,
    StrategyRuntimeStatus,
    create_connection_pool,
)
from trading_platform.shared.binance.live_executor import BinanceOrderExecutor
from trading_platform.shared.binance.rest_client import BinanceRestClient
from trading_platform.shared.binance.strategy_account import BinanceStrategyAccount
from trading_platform.shared.binance.symbol_rules import BinanceSymbolRuleBook
from trading_platform.shared.config import BinanceConfig, DatabaseConfig, StrategyConfig
from trading_platform.shared.events import Kline
from trading_platform.shared.execution_event_journal import (
    DurableExecutionEventJournal,
    ExecutionEvent,
)
from trading_platform.shared.execution_recovery import OrderWAL
from trading_platform.shared.logging_config import setup_logger
from trading_platform.shared.postgres_lease import PostgresExecutionLease
from trading_platform.shared.risk import RiskConfig, RiskGuard

from .core import LongBreakoutConfig, LongBreakoutStrategy
from .execution import LongBreakoutExecutionCoordinator
from .settings import LongBreakoutSettings


STRATEGY_ID = LongBreakoutStrategy.STRATEGY_ID
TESTNET_REST_URL = "https://demo-fapi.binance.com"
TESTNET_WS_URL = "wss://stream.binancefuture.com"
OPERATIONAL_GATES = ("lease", "execution", "market", "worker")
ENTRY_GATES = ("entry_config", "admission", "leverage", "margin", "capital")
logger = setup_logger("long_breakout")


class LongBreakoutRuntimeCallbacks:
    """Apply owned exchange events to the ledger, account view, then strategy."""

    def __init__(
        self,
        *,
        delegate: Any,
        account: BinanceStrategyAccount,
        coordinator: LongBreakoutExecutionCoordinator,
    ) -> None:
        self.delegate = delegate
        self.account = account
        self.coordinator = coordinator
        self._startup_ready = asyncio.Event()
        self._startup_failed = False

    def begin_startup_recovery(self) -> None:
        self._startup_failed = False
        self._startup_ready.clear()

    def finish_startup_recovery(self) -> None:
        self._startup_ready.set()

    def abort_startup_recovery(self) -> None:
        self._startup_failed = True
        self._startup_ready.set()

    async def _wait_for_startup_recovery(self) -> None:
        await self._startup_ready.wait()
        if self._startup_failed:
            raise RuntimeError("long_breakout startup recovery failed")

    async def handle_execution_report(self, order_data: dict[str, Any]) -> None:
        await self._wait_for_startup_recovery()
        await self.delegate.handle_execution_report(order_data)
        fill = self.account.handle_execution_report(order_data)
        record = self.account.wal.recover_latest().get(str(order_data.get("c") or ""))
        if record is None:
            raise RuntimeError("owned execution report is missing from long_breakout WAL")
        self.coordinator.on_order_update(record)
        if fill is None:
            return
        campaign_id = (
            str(record.payload.get("campaign_id"))
            if record.payload.get("campaign_id")
            else None
        )
        self.coordinator.on_fill(fill, campaign_id=campaign_id)

    async def handle_account_update(self, event: dict[str, Any]) -> None:
        await self._wait_for_startup_recovery()
        await self.delegate.handle_account_update(event)
        await self.account.handle_account_update(event)


class LongBreakoutProcess:
    """Run one long strategy against one dedicated Binance testnet account."""

    def __init__(
        self,
        settings: LongBreakoutSettings,
        *,
        binance: BinanceConfig,
        database: DatabaseConfig,
        strategy_config: StrategyConfig,
    ) -> None:
        if strategy_config.account_id != settings.account_id:
            raise ValueError(
                "STRATEGY_ACCOUNT_ID must match LONG_BREAKOUT_ACCOUNT_ID"
            )
        if not binance.testnet:
            raise ValueError("long_breakout runtime is testnet-only")
        if binance.base_url.rstrip("/") != TESTNET_REST_URL:
            raise ValueError("long_breakout requires the Binance Futures testnet REST URL")
        if binance.ws_base_url.rstrip("/") != TESTNET_WS_URL:
            raise ValueError("long_breakout requires the Binance Futures testnet WS URL")
        if not binance.api_key or not binance.api_secret:
            raise ValueError("dedicated Binance testnet credentials are required")
        if Decimal(str(strategy_config.risk_max_position_value_usdt)) < (
            settings.entry_notional_usdt
        ):
            raise ValueError(
                "strategy risk max position value must cover entry notional"
            )
        if strategy_config.risk_max_symbols < len(settings.symbol_list):
            raise ValueError("strategy risk max symbols must cover configured symbols")

        self.settings = settings
        self.binance = binance
        self.database = database
        self.strategy_config = strategy_config
        self.instance_id = uuid4().hex
        self.started_at = datetime.now(timezone.utc)
        self._stop = asyncio.Event()
        self._stop_lock = asyncio.Lock()
        self._stopped = False
        self._stack = AsyncExitStack()
        self._tasks: list[asyncio.Task[None]] = []
        self._gates = {
            name: False for name in (*OPERATIONAL_GATES, *ENTRY_GATES)
        }
        self._gates["entry_config"] = settings.entry_enabled
        self._admission_enabled = False
        self._fatal_reason: str | None = None
        self._execution_recovering = True

        self.db: LedgerDB | None = None
        self.rest: BinanceRestClient | None = None
        self.http: httpx.AsyncClient | None = None
        self.lease: PostgresExecutionLease | None = None
        self.runtime: Any = None
        self.runtime_callbacks: LongBreakoutRuntimeCallbacks | None = None
        self.account: BinanceStrategyAccount | None = None
        self.strategy: LongBreakoutStrategy | None = None
        self.coordinator: LongBreakoutExecutionCoordinator | None = None
        self.event_journal: DurableExecutionEventJournal | None = None

    @property
    def entry_enabled(self) -> bool:
        return (
            self.settings.entry_enabled
            and self._admission_enabled
            and self._fatal_reason is None
            and all(
                self._gates.get(name, False)
                for name in (*OPERATIONAL_GATES, *ENTRY_GATES)
            )
        )

    def request_stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        await self.start()
        await self._stop.wait()
        fatal_reason = self._fatal_reason
        await self.stop()
        if fatal_reason:
            raise RuntimeError(fatal_reason)

    async def start(self) -> None:
        if self._stopped:
            raise RuntimeError("long_breakout process is already stopped")
        if self.runtime is not None:
            return
        try:
            await self._build_resources()
            assert self.runtime is not None
            assert self.runtime_callbacks is not None
            assert self.account is not None
            assert self.coordinator is not None
            assert self.strategy is not None

            await self.runtime.start()
            await self.account.refresh_positions()
            await self.coordinator.restore_from_account()
            self._execution_recovering = False
            self._gates["execution"] = True
            self.runtime_callbacks.finish_startup_recovery()

            await self._register_market_subscription()
            await self._warm_market_history()
            self._gates["market"] = True
            await self._refresh_admission()

            worker_task = self.coordinator.start()
            self._gates["worker"] = True
            self._tasks.extend(
                [
                    asyncio.create_task(
                        self._market_loop(), name="long-breakout-market"
                    ),
                    asyncio.create_task(
                        self._heartbeat_loop(), name="long-breakout-heartbeat"
                    ),
                    asyncio.create_task(
                        self._watch_worker(worker_task),
                        name="long-breakout-worker-watch",
                    ),
                    asyncio.create_task(
                        self._watch_fatal(
                            self.runtime.user_stream.wait_fatal(),
                            "execution stream callback failed",
                        ),
                        name="long-breakout-stream-watch",
                    ),
                    asyncio.create_task(
                        self._watch_fatal(
                            self.runtime.unknown_poller.wait_fatal(),
                            "SUBMIT_UNKNOWN recovery failed",
                        ),
                        name="long-breakout-unknown-watch",
                    ),
                    asyncio.create_task(
                        self._watch_fatal(
                            self.lease.wait_lost(),
                            "execution account lease lost",
                        ),
                        name="long-breakout-lease-watch",
                    ),
                ]
            )
            self.strategy.set_entry_enabled(self.entry_enabled)
            await self._append_event(
                "runtime.started",
                source="long_breakout.runtime",
                details={"gates": dict(self._gates)},
            )
            await self._publish_runtime_status()
        except BaseException as exc:
            self._fatal_reason = f"startup failed: {type(exc).__name__}: {exc}"
            if self.runtime_callbacks is not None:
                self.runtime_callbacks.abort_startup_recovery()
            try:
                await self._append_event(
                    "runtime.start_failed",
                    source="long_breakout.runtime",
                    severity="critical",
                    details={
                        "error_type": type(exc).__name__,
                        "error_message": str(exc)[:500],
                    },
                )
            except BaseException:
                logger.exception("Failed to record long_breakout startup failure")
            await self.stop()
            raise

    async def stop(self) -> None:
        async with self._stop_lock:
            if self._stopped:
                return
            self._stopped = True
            await self._stop_once()

    async def _stop_once(self) -> None:
        errors: list[BaseException] = []
        if self.strategy is not None:
            self.strategy.set_entry_enabled(False)
        self._gates.update({name: False for name in OPERATIONAL_GATES})
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

        if self.runtime is not None:
            try:
                await self.runtime.unknown_poller.stop()
            except BaseException as exc:
                errors.append(exc)
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
        if self.db is not None:
            try:
                await self._publish_runtime_status(
                    status="fatal" if self._fatal_reason else "stopped",
                    stopped=True,
                )
            except BaseException as exc:
                errors.append(exc)
        try:
            await self._unregister_market_subscription()
        except BaseException as exc:
            errors.append(exc)
        try:
            await self._append_event(
                "runtime.stopped",
                source="long_breakout.runtime",
                severity="error" if errors else "info",
                details={
                    "fatal_reason": self._fatal_reason,
                    "shutdown_error_types": [type(exc).__name__ for exc in errors],
                },
            )
        except BaseException as exc:
            errors.append(exc)
        try:
            await self._stack.aclose()
        except BaseException as exc:
            errors.append(exc)
        self.runtime = None
        if errors:
            raise BaseExceptionGroup("long_breakout shutdown failed", errors)

    async def _build_resources(self) -> None:
        pool = await create_connection_pool(self.database.dsn)
        self._stack.push_async_callback(pool.close)
        self.lease = PostgresExecutionLease(pool, self.settings.account_id)
        await self.lease.acquire()
        self._stack.push_async_callback(self.lease.release)
        self._gates["lease"] = True
        self.db = LedgerDB(pool)

        self.event_journal = DurableExecutionEventJournal(
            f"{self.settings.wal_path}.events.jsonl",
            run_id=self.instance_id,
            account_id=self.settings.account_id,
            strategy_id=STRATEGY_ID,
            persist=self._persist_execution_events,
        )
        await self.event_journal.start()
        await self._append_event(
            "runtime.initializing",
            source="long_breakout.runtime",
            details={
                "mode": self.settings.mode,
                "symbols": self.settings.symbol_list,
                "timeframe": self.settings.timeframe,
            },
        )

        self.rest = BinanceRestClient(
            api_key=self.binance.api_key,
            api_secret=self.binance.api_secret,
            base_url=self.binance.base_url,
        )
        self._stack.push_async_callback(self.rest.close)
        self.http = httpx.AsyncClient(
            base_url=self.strategy_config.market_api_url,
            timeout=10.0,
        )
        self._stack.push_async_callback(self.http.aclose)
        await self._validate_account()

        exchange_info = await self.rest.get_exchange_info()
        symbol_rules = BinanceSymbolRuleBook.from_exchange_info(
            exchange_info, symbols=self.settings.symbol_list
        )
        risk = RiskGuard(
            self.settings.account_id,
            RiskConfig(
                max_position_value_usdt=Decimal(
                    str(self.strategy_config.risk_max_position_value_usdt)
                ),
                max_symbols=self.strategy_config.risk_max_symbols,
                max_leverage=self.settings.leverage,
            )
        )
        wal = OrderWAL(self.settings.wal_path)
        self.account = BinanceStrategyAccount(
            self.rest,
            wal,
            account_id=self.settings.account_id,
            strategy_id=STRATEGY_ID,
            risk_guard=risk,
        )
        self.strategy = LongBreakoutStrategy(
            symbols=self.settings.symbol_list,
            config=LongBreakoutConfig(
                timeframe=self.settings.timeframe,
                lookback_bars=self.settings.lookback_bars,
                quantity=Decimal("1"),
                breakout_buffer_pct=self.settings.breakout_buffer_pct,
                take_profit_pct=self.settings.take_profit_pct,
                stop_loss_pct=self.settings.stop_loss_pct,
                entry_order_type="MARKET",
                exit_order_type="MARKET",
            ),
        )
        self.strategy.set_entry_enabled(False)
        executor = BinanceOrderExecutor(
            self.rest,
            wal,
            account_id=self.settings.account_id,
            risk_guard=risk,
            symbol_rules=symbol_rules,
            can_open_symbol=lambda _symbol: self.entry_enabled,
        )
        self.coordinator = LongBreakoutExecutionCoordinator(
            strategy=self.strategy,
            account=self.account,
            executor=executor,
            entry_notional_usdt=self.settings.entry_notional_usdt,
            leverage=self.settings.leverage,
            entry_allowed=lambda: self.entry_enabled,
            account_id=self.settings.account_id,
            trade_source=self.db,
        )
        self.runtime = create_binance_execution_runtime(
            rest_client=self.rest,
            executor=executor,
            db=self.db,
            account_id=self.settings.account_id,
            strategy_id=STRATEGY_ID,
            managed_symbols=self.settings.symbol_list,
            dedicated_strategy_account=self.settings.dedicated_strategy_account,
            ws_base_url=self.binance.ws_base_url,
            poll_interval_seconds=self.settings.poll_interval_seconds,
            max_poll_attempts=self.settings.max_poll_attempts,
            on_raw_event=self._observe_binance_event,
            reconciliation_observer=self._observe_reconciliation,
        )
        delegate = SimpleNamespace(
            handle_execution_report=self.runtime.user_stream.on_execution_report,
            handle_account_update=self.runtime.user_stream.on_account_update,
        )
        self.runtime_callbacks = LongBreakoutRuntimeCallbacks(
            delegate=delegate,
            account=self.account,
            coordinator=self.coordinator,
        )
        self.runtime_callbacks.begin_startup_recovery()
        self.runtime.on_startup_failure = self.runtime_callbacks.abort_startup_recovery
        self.runtime.user_stream.on_execution_report = (
            self.runtime_callbacks.handle_execution_report
        )
        self.runtime.user_stream.on_account_update = (
            self.runtime_callbacks.handle_account_update
        )
        self.runtime.user_stream.on_disconnect = self._on_execution_disconnected
        self.runtime.on_recovered = self._on_execution_recovered

    async def _validate_account(self) -> None:
        assert self.rest is not None
        account = await self.rest.get_account()
        if account.get("canTrade") is not True:
            raise RuntimeError("Binance testnet account cannot trade")
        try:
            available_balance = Decimal(str(account.get("availableBalance")))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise RuntimeError("Binance account has invalid availableBalance") from exc
        self._gates["capital"] = (
            available_balance
            >= self.settings.entry_notional_usdt / self.settings.leverage
        )
        position_mode = await self.rest.get_position_mode()
        if position_mode.get("dualSidePosition") is not False:
            raise RuntimeError("long_breakout requires Binance one-way mode")
        leverage_ready = True
        margin_ready = True
        for symbol in self.settings.symbol_list:
            rows = await self.rest.get_position_risk(symbol)
            if not isinstance(rows, list) or len(rows) != 1:
                raise RuntimeError(f"invalid Binance position risk for {symbol}")
            row = rows[0]
            if str(row.get("marginType") or "").lower() not in {"cross", "crossed"}:
                margin_ready = False
            try:
                leverage = int(row.get("leverage"))
            except (TypeError, ValueError) as exc:
                raise RuntimeError(f"invalid Binance leverage for {symbol}") from exc
            if leverage != self.settings.leverage:
                leverage_ready = False
                logger.warning(
                    "Entry disabled for %s: expected leverage %s, got %s",
                    symbol,
                    self.settings.leverage,
                    leverage,
                )
        self._gates["leverage"] = leverage_ready
        self._gates["margin"] = margin_ready

    async def _register_market_subscription(self) -> None:
        assert self.http is not None
        health = await self.http.get("/health")
        health.raise_for_status()
        payload = health.json()
        if payload.get("binance_testnet") is not True:
            raise RuntimeError("Market and long_breakout must use the same testnet")
        response = await self.http.put(
            f"/subscriptions/{self._consumer_id}",
            json={
                "symbols": self.settings.symbol_list,
                "types": [f"kline:{self.settings.timeframe}"],
            },
        )
        response.raise_for_status()

    async def _unregister_market_subscription(self) -> None:
        if self.http is None:
            return
        response = await self.http.delete(f"/subscriptions/{self._consumer_id}")
        if response.status_code not in {200, 404}:
            response.raise_for_status()

    @property
    def _consumer_id(self) -> str:
        return f"long_breakout_{self.settings.account_id}"

    async def _warm_market_history(self) -> None:
        assert self.strategy is not None
        required = self.settings.lookback_bars or 1
        for symbol in self.settings.symbol_list:
            # Binance counts the current incomplete candle toward ``limit``;
            # Market correctly filters it, so request one extra candidate.
            values = await self._fetch_klines(symbol, limit=required + 1)
            if len(values) < required:
                raise RuntimeError(f"insufficient 7-day K-line warmup for {symbol}")
            self.strategy.seed_history(values[-required:])

    async def _fetch_klines(self, symbol: str, *, limit: int) -> list[Kline]:
        assert self.http is not None
        response = await self.http.get(
            f"/klines/{symbol}/{self.settings.timeframe}",
            params={"limit": limit},
        )
        response.raise_for_status()
        payload = response.json()
        raw_klines = payload.get("klines")
        if not isinstance(raw_klines, list):
            raise RuntimeError("Market returned invalid historical K-lines")
        values = [Kline.from_dict(value) for value in raw_klines]
        if any(
            value.symbol != symbol or value.interval != self.settings.timeframe
            for value in values
        ):
            raise RuntimeError("Market returned K-lines with mismatched identity")
        return sorted(values, key=lambda value: value.close_time)

    async def _market_loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.market_poll_seconds)
            try:
                await self._refresh_admission()
                for symbol in self.settings.symbol_list:
                    await self._process_latest(symbol)
                self._gates["market"] = True
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                self._gates["market"] = False
                if self.strategy is not None:
                    self.strategy.set_entry_enabled(False)
                logger.warning(
                    "long_breakout market poll failed: %s: %s",
                    type(exc).__name__,
                    exc,
                )

    async def _process_latest(self, symbol: str) -> None:
        assert self.strategy is not None
        assert self.coordinator is not None
        values = await self._fetch_klines(
            symbol, limit=(self.settings.lookback_bars or 1) + 1
        )
        last_close_time = self.strategy.last_close_time(symbol)
        pending = [
            value
            for value in values
            if last_close_time is None or value.close_time > last_close_time
        ]
        if not pending:
            self.strategy.set_entry_enabled(self.entry_enabled)
            return
        if (
            last_close_time is None
            or pending[0].open_time != last_close_time + 1
        ):
            self.strategy.seed_history(values[-(self.settings.lookback_bars or 1) :])
            self._gates["market"] = False
            self.strategy.set_entry_enabled(False)
            await self._append_event(
                "market.kline_gap_skipped",
                source="long_breakout.market",
                severity="warning",
                symbol=symbol,
                details={
                    "previous_close_time": last_close_time,
                    "next_available_open_time": pending[0].open_time,
                    "reseeded_bars": min(
                        len(values), self.settings.lookback_bars or 1
                    ),
                },
            )
            raise RuntimeError(f"K-line gap recovered without trading: {symbol}")
        self.strategy.set_entry_enabled(self.entry_enabled)
        for kline in pending:
            intents = self.strategy.on_kline(kline)
            self.coordinator.enqueue(intents, event_time=kline.available_time)

    async def _refresh_admission(self) -> None:
        assert self.db is not None
        self._admission_enabled = await self.db.is_subcategory_enabled(
            self.settings.subcategory
        )
        self._gates["admission"] = self._admission_enabled
        if self.strategy is not None:
            self.strategy.set_entry_enabled(self.entry_enabled)

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.heartbeat_seconds)
            self._gates["execution"] = bool(
                self.runtime is not None
                and self.runtime.is_running
                and self.runtime.user_stream.connected
                and not self._execution_recovering
            )
            self._gates["worker"] = bool(
                self.coordinator is not None
                and self.coordinator.worker_task is not None
                and not self.coordinator.worker_task.done()
            )
            try:
                await self._publish_runtime_status()
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                self._mark_fatal(f"runtime heartbeat failed: {type(exc).__name__}")
                return

    async def _watch_worker(self, worker_task: asyncio.Task[None]) -> None:
        try:
            await asyncio.shield(worker_task)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            self._gates["worker"] = False
            self._mark_fatal(f"execution worker failed: {type(exc).__name__}")
        else:
            self._gates["worker"] = False
            self._mark_fatal("execution worker stopped unexpectedly")

    async def _watch_fatal(
        self, awaitable: Awaitable[BaseException], reason: str
    ) -> None:
        try:
            exc = await awaitable
        except asyncio.CancelledError:
            raise
        self._mark_fatal(f"{reason}: {type(exc).__name__}")

    def _mark_fatal(self, reason: str) -> None:
        if self._fatal_reason is None:
            self._fatal_reason = reason
        if self.strategy is not None:
            self.strategy.set_entry_enabled(False)
        self._stop.set()

    def _on_execution_disconnected(self) -> None:
        self._execution_recovering = True
        self._gates["execution"] = False
        if self.runtime_callbacks is not None:
            self.runtime_callbacks.begin_startup_recovery()
        if self.strategy is not None:
            self.strategy.set_entry_enabled(False)

    async def _on_execution_recovered(self) -> None:
        assert self.account is not None
        assert self.coordinator is not None
        assert self.runtime_callbacks is not None
        await self._validate_account()
        await self.account.refresh_positions()
        await self.coordinator.restore_from_account()
        self._execution_recovering = False
        self._gates["execution"] = True
        if self.strategy is not None:
            self.strategy.set_entry_enabled(self.entry_enabled)
        self.runtime_callbacks.finish_startup_recovery()

    async def _publish_runtime_status(
        self, *, status: str | None = None, stopped: bool = False
    ) -> None:
        if self.db is None:
            return
        now = datetime.now(timezone.utc)
        if status is None:
            operational = all(self._gates.get(name, False) for name in OPERATIONAL_GATES)
            status = "running" if operational and not self._fatal_reason else "degraded"
        accepted = await self.db.upsert_strategy_runtime_status(
            StrategyRuntimeStatus(
                account_id=self.settings.account_id,
                strategy_id=STRATEGY_ID,
                instance_id=self.instance_id,
                mode=self.settings.mode,
                status=status,
                entry_enabled=self.entry_enabled,
                halted=self._fatal_reason is not None,
                halt_reason=self._fatal_reason,
                gate_conditions=dict(self._gates),
                started_at=self.started_at,
                heartbeat_at=now,
                stopped_at=now if stopped else None,
            )
        )
        if not accepted:
            self._mark_fatal("runtime status ownership lost to another instance")
            raise RuntimeError("runtime status ownership lost to another instance")

    async def _persist_execution_events(
        self, events: tuple[ExecutionEvent, ...]
    ) -> int:
        if self.db is None:
            raise RuntimeError("execution event database is unavailable")
        return await self.db.insert_execution_events(events)

    async def _append_event(
        self,
        event_type: str,
        *,
        source: str,
        severity: str = "info",
        trace_id: str | None = None,
        symbol: str | None = None,
        client_order_id: str | None = None,
        exchange_order_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        if self.event_journal is None:
            return
        await self.event_journal.append(
            event_type,
            source=source,
            severity=severity,
            trace_id=trace_id,
            symbol=symbol,
            client_order_id=client_order_id,
            exchange_order_id=exchange_order_id,
            details=details or {},
        )

    async def _observe_binance_event(self, event: dict[str, Any]) -> None:
        order = event.get("o") if isinstance(event.get("o"), dict) else {}
        await self._append_event(
            "binance.user_stream.received",
            source="binance.user_stream",
            trace_id=str(order.get("c") or "") or None,
            symbol=str(order.get("s") or "") or None,
            client_order_id=str(order.get("c") or "") or None,
            exchange_order_id=str(order.get("i") or "") or None,
            details={"event_type": event.get("e"), "envelope": event},
        )

    async def _observe_reconciliation(
        self, stage: str, details: dict[str, Any]
    ) -> None:
        await self._append_event(
            f"runtime.reconciliation_{stage}",
            source="long_breakout.reconciliation",
            severity="error" if stage == "failed" else "info",
            trace_id=str(details.get("trace_id") or "") or None,
            details=details,
        )


async def async_main() -> None:
    settings = LongBreakoutSettings()
    binance = BinanceConfig(testnet=True)
    process = LongBreakoutProcess(
        settings,
        binance=binance,
        database=DatabaseConfig(),
        strategy_config=StrategyConfig(),
    )
    loop = asyncio.get_running_loop()
    for name in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(name, process.request_stop)
    await process.run()


def main() -> None:
    asyncio.run(async_main())
