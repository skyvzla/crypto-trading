"""Reliable execution adapter for the long-breakout strategy core."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from decimal import Decimal
from typing import Any, Callable, Iterable, Protocol

from trading_platform.shared.binance.live_executor import BinanceOrderExecutor
from trading_platform.shared.binance.strategy_account import BinanceStrategyAccount
from trading_platform.shared.events import Fill, OrderIntent
from trading_platform.shared.execution_recovery import OrderWALRecord
from trading_platform.strategies.execution_queue import (
    ExecutionJob,
    ExecutionQueue,
    ExecutionWorker,
)

from .core import LongBreakoutStrategy


_ACTIVE_ORDER_STATUSES = {"NEW", "PARTIALLY_FILLED", "SUBMIT_UNKNOWN"}
_REJECTED_ORDER_STATUSES = {"CANCELLED", "EXPIRED", "REJECTED"}
_TERMINAL_ORDER_STATUSES = _REJECTED_ORDER_STATUSES | {"FILLED"}
_CANCELLATION_DRAIN_TIMEOUT_SECONDS = 90
_SUBMISSION_SETTLE_TIMEOUT_SECONDS = 30


class LongBreakoutTradeSource(Protocol):
    async def get_trades_by_client_order_ids(
        self,
        *,
        account_id: str,
        strategy_id: str,
        symbol: str,
        campaign_id: str,
        client_order_ids: list[str],
    ) -> list[Any]: ...


class LongBreakoutExecutionCoordinator:
    """Keep signal processing synchronous while exchange I/O runs in one worker."""

    def __init__(
        self,
        *,
        strategy: LongBreakoutStrategy,
        account: BinanceStrategyAccount,
        executor: BinanceOrderExecutor,
        entry_notional_usdt: Decimal,
        leverage: int,
        entry_allowed: Callable[[], bool],
        account_id: str,
        trade_source: LongBreakoutTradeSource,
        execution_queue: ExecutionQueue | None = None,
    ) -> None:
        if entry_notional_usdt <= 0:
            raise ValueError("entry_notional_usdt must be positive")
        if leverage <= 0:
            raise ValueError("leverage must be positive")
        self.strategy = strategy
        self.account = account
        self.executor = executor
        self.entry_notional_usdt = entry_notional_usdt
        self.leverage = leverage
        self.entry_allowed = entry_allowed
        self.account_id = account_id
        self.trade_source = trade_source
        self.execution_queue = execution_queue or ExecutionQueue()
        self.worker = ExecutionWorker(
            self.execution_queue,
            self._handle_execution_job,
            task_name="long-breakout-execution-worker",
        )
        self.worker_task: asyncio.Task[None] | None = None
        self._stop_lock = asyncio.Lock()
        self._stopped = False

    def start(self) -> asyncio.Task[None]:
        if self._stopped:
            raise RuntimeError("long_breakout execution coordinator is stopped")
        self.worker_task = self.worker.start()
        return self.worker_task

    def enqueue(self, intents: Iterable[OrderIntent], *, event_time: int) -> int:
        queued = 0
        for intent in intents:
            self._validate_intent(intent)
            if not intent.reduce_only and (
                self._stopped or not self.entry_allowed()
            ):
                self.strategy.clear_pending_entry(intent.symbol)
                continue
            try:
                self.execution_queue.put_nowait(
                    "exit" if intent.reduce_only else "entry",
                    intent=intent,
                    event_time=event_time,
                )
            except BaseException:
                if intent.reduce_only:
                    self.strategy.clear_pending_exit(intent.symbol)
                else:
                    self.strategy.clear_pending_entry(intent.symbol)
                raise
            queued += 1
        return queued

    async def _handle_execution_job(self, job: ExecutionJob) -> None:
        if job.kind == "cancel":
            await self._flush_cancellations()
            return
        intent = job.intent
        if intent is None:
            raise RuntimeError("execution job is missing its intent")
        if not intent.reduce_only:
            if self._stopped or not self.entry_allowed():
                self.strategy.clear_pending_entry(intent.symbol)
                return
            intent = replace(
                intent,
                quantity=self.entry_notional_usdt / intent.price,
            )
        record = await self.executor.submit(
            intent,
            reference_price=intent.price,
            leverage=self.leverage,
        )
        if record.status in _REJECTED_ORDER_STATUSES:
            self._apply_terminal_order(record)

    def on_fill(self, fill: Fill, *, campaign_id: str | None) -> None:
        self.strategy.on_fill(fill, campaign_id=campaign_id)

    def on_order_update(self, record: OrderWALRecord) -> None:
        self._apply_terminal_order(record)

    async def restore_from_account(self) -> None:
        """Restore positions and active orders after strict startup reconciliation."""

        self.strategy.reset_execution_state()
        latest_records = self.account.wal.recover_latest()
        for symbol in self.account.symbols_with_live_risk():
            position = self.account.get_position(symbol)
            campaign_id = self._latest_entry_campaign(symbol, latest_records.values())
            if position is not None:
                if position.side != "LONG":
                    raise RuntimeError(
                        f"long_breakout account contains a non-LONG position: {symbol}"
                    )
                if campaign_id is None:
                    raise RuntimeError(
                        f"open LONG position has no owned entry campaign in WAL: {symbol}"
                    )
                self.strategy.restore_position(
                    symbol,
                    position.quantity,
                    position.entry_price,
                    campaign_id,
                )
                await self._restore_trade_state(
                    symbol, campaign_id, latest_records.values()
                )
        for order in self.account.iter_orders():
            if order.status not in _ACTIVE_ORDER_STATUSES:
                continue
            expected_side = "SELL" if order.reduce_only else "BUY"
            if order.side != expected_side:
                raise RuntimeError(
                    "active long_breakout order has invalid side: "
                    f"{order.client_order_id}"
                )
            campaign_id = order.campaign_id
            if not campaign_id:
                raise RuntimeError(
                    f"active owned order has no campaign id: {order.client_order_id}"
                )
            self.strategy.restore_pending_order(
                order.symbol,
                client_order_id=order.client_order_id,
                campaign_id=campaign_id,
                reduce_only=order.reduce_only,
            )

    async def _restore_trade_state(
        self,
        symbol: str,
        campaign_id: str,
        records: Iterable[OrderWALRecord],
    ) -> None:
        owned_records = [
            record
            for record in records
            if record.account_id == self.account_id
            and record.symbol == symbol
            and record.payload.get("strategy_id") == LongBreakoutStrategy.STRATEGY_ID
            and record.payload.get("campaign_id") == campaign_id
        ]
        client_order_ids = sorted(
            {record.client_order_id for record in owned_records}
        )
        entry_ids = {
            record.client_order_id
            for record in owned_records
            if record.side == "BUY"
            and not bool(record.payload.get("reduce_only", False))
        }
        if not entry_ids:
            raise RuntimeError("recovered LONG has no owned entry order in WAL")
        trades = await self.trade_source.get_trades_by_client_order_ids(
            account_id=self.account_id,
            strategy_id=LongBreakoutStrategy.STRATEGY_ID,
            symbol=symbol,
            campaign_id=campaign_id,
            client_order_ids=client_order_ids,
        )
        if not trades or not any(
            trade.client_order_id in entry_ids and trade.side == "BUY"
            for trade in trades
        ):
            raise RuntimeError("recovered LONG has no PostgreSQL entry trade")
        if any(
            trade.campaign_id != campaign_id
            or trade.client_order_id not in client_order_ids
            for trade in trades
        ):
            raise RuntimeError("PostgreSQL returned a trade outside the owned campaign")
        commission = sum(
            (Decimal(str(trade.commission or 0)) for trade in trades),
            start=Decimal("0"),
        )
        self.account.restore_trade_state(
            symbol,
            commission,
            {str(trade.trade_id) for trade in trades},
        )

    async def stop(self) -> None:
        async with self._stop_lock:
            if self._stopped:
                return
            self._stopped = True
            self.strategy.set_entry_enabled(False)
            try:
                async with asyncio.timeout(_CANCELLATION_DRAIN_TIMEOUT_SECONDS):
                    drain_error: RuntimeError | None = None
                    try:
                        async with asyncio.timeout(
                            _SUBMISSION_SETTLE_TIMEOUT_SECONDS
                        ):
                            await self._wait_for_execution_queue()
                    except TimeoutError:
                        # BinanceOrderExecutor records a cancelled in-flight
                        # submission as SUBMIT_UNKNOWN before the worker exits.
                        await self.worker.stop()
                        self.worker_task = None
                    except RuntimeError as exc:
                        drain_error = exc
                        await self.worker.stop()
                        self.worker_task = None

                    resolutions = (
                        await self.executor.resolve_recovered_unknowns_once()
                    )
                    unresolved = sorted(
                        client_order_id
                        for client_order_id, result in resolutions.items()
                        if not result.resolved
                    )
                    self._request_entry_cancellations()
                    if self.account.has_pending_cancellations:
                        await self._drain_cancellations()
                    if unresolved:
                        if drain_error is not None:
                            drain_error.add_note(
                                "shutdown also found unresolved order submissions: "
                                + ", ".join(unresolved)
                            )
                            raise drain_error
                        raise RuntimeError(
                            "order submissions remain unresolved at shutdown: "
                            + ", ".join(unresolved)
                        )
                    if drain_error is not None:
                        raise drain_error
            finally:
                await self.worker.stop()
                self.worker_task = None

    def _request_entry_cancellations(self) -> None:
        for order in self.account.iter_orders():
            if not order.reduce_only and order.status in {"NEW", "PARTIALLY_FILLED"}:
                self.account.cancel_order(order.order_id)

    async def _wait_for_execution_queue(self) -> None:
        worker_task = self.worker_task
        if worker_task is None:
            if self.execution_queue.qsize:
                raise RuntimeError("execution queue has no running worker")
            return

        joined = asyncio.create_task(
            self.execution_queue.join(), name="long-breakout-execution-drain"
        )
        try:
            done, _ = await asyncio.wait(
                {joined, worker_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if worker_task in done and not joined.done():
                raise RuntimeError("execution worker stopped before queue drain")
            await joined
            if worker_task.done():
                if worker_task.cancelled():
                    raise RuntimeError("execution worker was cancelled during queue drain")
                error = worker_task.exception()
                if error is not None:
                    raise RuntimeError(
                        "execution worker failed during queue drain"
                    ) from error
                raise RuntimeError("execution worker stopped during queue drain")
        finally:
            if not joined.done():
                joined.cancel()
                await asyncio.gather(joined, return_exceptions=True)

    async def _drain_cancellations(self) -> None:
        worker_task = self.worker_task
        if worker_task is None or worker_task.done():
            await self._flush_cancellations()
            if (
                worker_task is not None
                and not worker_task.cancelled()
                and worker_task.exception() is not None
            ):
                raise RuntimeError(
                    "execution worker failed before cancellation drain"
                ) from worker_task.exception()
            return

        self.execution_queue.put_nowait("cancel")
        joined = asyncio.create_task(
            self.execution_queue.join(), name="long-breakout-cancel-drain"
        )
        try:
            done, _ = await asyncio.wait(
                {joined, worker_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if worker_task in done and not joined.done():
                await self._flush_cancellations()
            else:
                await joined
                if worker_task.done() and self.account.has_pending_cancellations:
                    await self._flush_cancellations()
        finally:
            if not joined.done():
                joined.cancel()
                await asyncio.gather(joined, return_exceptions=True)

        if worker_task.done() and not worker_task.cancelled():
            error = worker_task.exception()
            if error is not None:
                raise RuntimeError(
                    "execution worker failed while draining cancellations"
                ) from error

    async def _flush_cancellations(self) -> None:
        cancelled = await self.account.flush_cancellations()
        for client_order_id in cancelled:
            order = next(
                (
                    item
                    for item in self.account.iter_orders()
                    if item.client_order_id == client_order_id
                ),
                None,
            )
            if order is not None:
                self.strategy.on_order_cancelled(
                    order.symbol, reduce_only=order.reduce_only
                )
        if self.account.has_pending_cancellations:
            raise RuntimeError("entry cancellation remains unresolved")

    def _apply_terminal_order(self, record: OrderWALRecord) -> None:
        if record.status not in _TERMINAL_ORDER_STATUSES:
            return
        self.strategy.on_order_terminal(
            record.symbol,
            client_order_id=record.client_order_id,
            reduce_only=bool(record.payload.get("reduce_only", False)),
        )

    @staticmethod
    def _latest_entry_campaign(
        symbol: str, records: Iterable[OrderWALRecord]
    ) -> str | None:
        candidates = [
            record
            for record in records
            if record.symbol == symbol
            and record.side == "BUY"
            and not bool(record.payload.get("reduce_only", False))
            and isinstance(record.payload.get("campaign_id"), str)
        ]
        if not candidates:
            return None
        latest = max(candidates, key=lambda record: record.recorded_at)
        return str(latest.payload["campaign_id"])

    @staticmethod
    def _validate_intent(intent: OrderIntent) -> None:
        if intent.strategy_id != LongBreakoutStrategy.STRATEGY_ID:
            raise ValueError("order intent strategy_id does not match long_breakout")
        expected_side = "SELL" if intent.reduce_only else "BUY"
        if intent.side != expected_side:
            raise ValueError("long_breakout order intent side is invalid")
        prefix = f"{LongBreakoutStrategy.STRATEGY_ID}:{intent.symbol}:"
        if not intent.campaign_id or not intent.campaign_id.startswith(prefix):
            raise ValueError("long_breakout order intent campaign_id is invalid")
