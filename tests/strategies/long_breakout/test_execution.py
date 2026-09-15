import asyncio
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from trading_platform.shared.binance.live_executor import BinanceOrderExecutor
from trading_platform.shared.binance.strategy_account import BinanceStrategyAccount
from trading_platform.shared.events import Fill, OrderIntent, Position
from trading_platform.shared.execution_recovery import OrderWAL, OrderWALRecord
from trading_platform.shared.risk import RiskConfig, RiskGuard
from trading_platform.strategies.execution_queue import ExecutionQueue
from trading_platform.strategies.long_breakout import LongBreakoutStrategy
from trading_platform.strategies.long_breakout import execution as execution_module
from trading_platform.strategies.long_breakout.execution import (
    LongBreakoutExecutionCoordinator,
)


ACCOUNT_ID = "long-breakout-test"
SYMBOL = "BTCUSDT"
CAMPAIGN = "long_breakout:BTCUSDT:1700000000000"


def entry_intent(*, symbol: str = SYMBOL, campaign_id: str = CAMPAIGN) -> OrderIntent:
    return OrderIntent(
        symbol=symbol,
        side="BUY",
        price=Decimal("125"),
        quantity=Decimal("1"),
        client_order_id=f"lgb-{symbol}-1700000000000-e",
        strategy_id="long_breakout",
        campaign_id=campaign_id,
    )


def exit_intent(*, symbol: str = SYMBOL, campaign_id: str = CAMPAIGN) -> OrderIntent:
    return OrderIntent(
        symbol=symbol,
        side="SELL",
        price=Decimal("130"),
        quantity=Decimal("1"),
        client_order_id=f"lgb-{symbol}-1700000000001-t",
        reduce_only=True,
        strategy_id="long_breakout",
        campaign_id=campaign_id,
    )


def wal_entry(campaign_id: str = CAMPAIGN) -> OrderWALRecord:
    return OrderWALRecord(
        record_type="exchange_status",
        recorded_at=1_000,
        account_id=ACCOUNT_ID,
        client_order_id="lgb-BTCUSDT-1700000000000-e",
        symbol=SYMBOL,
        side="BUY",
        order_type="MARKET",
        quantity="8",
        price="125",
        status="FILLED",
        exchange_order_id="exchange-1",
        payload={
            "strategy_id": "long_breakout",
            "reduce_only": False,
            "campaign_id": campaign_id,
        },
    )


def entry_trade(campaign_id: str = CAMPAIGN):
    return SimpleNamespace(
        trade_id="trade-1",
        client_order_id="lgb-BTCUSDT-1700000000000-e",
        campaign_id=campaign_id,
        side="BUY",
        commission=Decimal("0.01"),
    )


def account_mock(*, position=None, records=()):
    account = Mock()
    account.wal = Mock()
    account.wal.recover_latest.return_value = {
        record.client_order_id: record for record in records
    }
    account.symbols_with_live_risk.return_value = {SYMBOL} if position else set()
    account.get_position.return_value = position
    account.iter_orders.return_value = ()
    account.has_pending_cancellations = False
    return account


def coordinator(
    *,
    entry_allowed=True,
    account=None,
    strategy=None,
    executor=None,
    trade_source=None,
):
    strategy = strategy or LongBreakoutStrategy(symbol=SYMBOL)
    account = account or account_mock()
    executor = executor or Mock(
        resolve_recovered_unknowns_once=AsyncMock(return_value={})
    )
    trade_source = trade_source or Mock(
        get_trades_by_client_order_ids=AsyncMock(return_value=[entry_trade()])
    )
    return LongBreakoutExecutionCoordinator(
        strategy=strategy,
        account=account,
        executor=executor,
        entry_notional_usdt=Decimal("1000"),
        leverage=3,
        entry_allowed=lambda: entry_allowed() if callable(entry_allowed) else entry_allowed,
        account_id=ACCOUNT_ID,
        trade_source=trade_source,
    )


@pytest.mark.asyncio
async def test_worker_converts_entry_notional_to_quantity_and_keeps_buy_direction():
    executor = Mock()
    executor.submit = AsyncMock(return_value=SimpleNamespace(status="NEW", payload={}))
    strategy = LongBreakoutStrategy(symbol=SYMBOL)
    coordinator_instance = coordinator(strategy=strategy, executor=executor)
    job = coordinator_instance.execution_queue.put_nowait(
        "entry", intent=entry_intent(), event_time=1
    )
    queued = await coordinator_instance.execution_queue.get()

    await coordinator_instance._handle_execution_job(queued)
    coordinator_instance.execution_queue.task_done()

    submitted = executor.submit.await_args.args[0]
    assert submitted.side == "BUY"
    assert submitted.reduce_only is False
    assert submitted.quantity == Decimal("8")
    assert executor.submit.await_args.kwargs == {
        "reference_price": Decimal("125"),
        "leverage": 3,
    }
    assert job.intent is not None
    assert job.intent.quantity == Decimal("1")


def test_enqueue_accepts_buy_entry_and_reduce_only_sell_exit_but_rejects_wrong_sides():
    coordinator_instance = coordinator()
    assert coordinator_instance.enqueue(
        [entry_intent(), exit_intent()], event_time=123
    ) == 2

    wrong_entry = entry_intent()
    wrong_entry.side = "SELL"
    with pytest.raises(ValueError, match="side is invalid"):
        coordinator_instance._validate_intent(wrong_entry)

    wrong_exit = exit_intent()
    wrong_exit.side = "BUY"
    with pytest.raises(ValueError, match="side is invalid"):
        coordinator_instance._validate_intent(wrong_exit)


def test_enqueue_does_not_leave_pending_entry_when_entry_gate_is_disabled():
    strategy = LongBreakoutStrategy(symbol=SYMBOL)
    strategy.restore_pending_order(
        SYMBOL,
        client_order_id=entry_intent().client_order_id,
        campaign_id=CAMPAIGN,
        reduce_only=False,
    )
    coordinator_instance = coordinator(entry_allowed=False, strategy=strategy)

    assert coordinator_instance.enqueue([entry_intent()], event_time=1) == 0
    assert strategy.pending_entry(SYMBOL) is None


@pytest.mark.asyncio
async def test_worker_rechecks_entry_gate_before_submit():
    allowed = True
    executor = Mock()
    executor.submit = AsyncMock(return_value=SimpleNamespace(status="NEW", payload={}))
    strategy = LongBreakoutStrategy(symbol=SYMBOL)
    coordinator_instance = LongBreakoutExecutionCoordinator(
        strategy=strategy,
        account=account_mock(),
        executor=executor,
        entry_notional_usdt=Decimal("1000"),
        leverage=3,
        entry_allowed=lambda: allowed,
        account_id=ACCOUNT_ID,
        trade_source=Mock(),
    )
    queued = coordinator_instance.execution_queue.put_nowait(
        "entry", intent=entry_intent(), event_time=1
    )
    allowed = False
    job = await coordinator_instance.execution_queue.get()
    await coordinator_instance._handle_execution_job(job)
    coordinator_instance.execution_queue.task_done()

    executor.submit.assert_not_awaited()
    assert queued.intent is not None


@pytest.mark.asyncio
async def test_restore_from_account_restores_owned_long_and_rejects_short():
    long_position = Position(
        symbol=SYMBOL,
        side="LONG",
        entry_price=Decimal("125"),
        quantity=Decimal("8"),
        total_commission=Decimal("0"),
        unrealized_pnl=Decimal("0"),
        realized_pnl=Decimal("0"),
        opened_at=1_000,
    )
    strategy = LongBreakoutStrategy(symbol=SYMBOL)
    coordinator_instance = coordinator(
        strategy=strategy,
        account=account_mock(position=long_position, records=(wal_entry(),)),
    )

    await coordinator_instance.restore_from_account()

    restored = strategy.position(SYMBOL)
    assert restored is not None
    assert restored.quantity == Decimal("8")
    assert restored.entry_price == Decimal("125")
    assert restored.campaign_id == CAMPAIGN

    short_position = Position(
        symbol=SYMBOL,
        side="SHORT",
        entry_price=Decimal("125"),
        quantity=Decimal("8"),
        total_commission=Decimal("0"),
        unrealized_pnl=Decimal("0"),
        realized_pnl=Decimal("0"),
        opened_at=1_000,
    )
    with pytest.raises(RuntimeError, match="non-LONG"):
        await coordinator(
            strategy=LongBreakoutStrategy(symbol=SYMBOL),
            account=account_mock(position=short_position, records=(wal_entry(),)),
        ).restore_from_account()


@pytest.mark.asyncio
async def test_restore_from_account_replaces_stale_strategy_execution_state():
    strategy = LongBreakoutStrategy(symbol=SYMBOL)
    strategy.restore_position(SYMBOL, Decimal("2"), Decimal("100"), CAMPAIGN)
    strategy.restore_pending_order(
        SYMBOL,
        client_order_id="stale-exit",
        campaign_id=CAMPAIGN,
        reduce_only=True,
    )

    await coordinator(strategy=strategy, account=account_mock()).restore_from_account()

    assert strategy.position(SYMBOL) is None
    assert strategy.pending_exit(SYMBOL) is None


def test_terminal_order_update_only_clears_the_matching_pending_order():
    strategy = LongBreakoutStrategy(symbol=SYMBOL)
    strategy.restore_pending_order(
        SYMBOL,
        client_order_id="current-entry",
        campaign_id=CAMPAIGN,
        reduce_only=False,
    )
    coordinator_instance = coordinator(strategy=strategy)

    old = replace(
        wal_entry(), status="CANCELLED", client_order_id="old-entry"
    )
    coordinator_instance.on_order_update(old)
    assert strategy.pending_entry(SYMBOL) == "current-entry"

    current = replace(
        wal_entry(), status="EXPIRED", client_order_id="current-entry"
    )
    coordinator_instance.on_order_update(current)
    assert strategy.pending_entry(SYMBOL) is None


def test_partial_buy_fill_keeps_pending_entry_until_filled_order_update():
    strategy = LongBreakoutStrategy(symbol=SYMBOL)
    client_order_id = wal_entry().client_order_id
    strategy.restore_pending_order(
        SYMBOL,
        client_order_id=client_order_id,
        campaign_id=CAMPAIGN,
        reduce_only=False,
    )
    coordinator_instance = coordinator(strategy=strategy)

    coordinator_instance.on_order_update(
        replace(wal_entry(), status="PARTIALLY_FILLED")
    )
    coordinator_instance.on_fill(
        Fill(
            fill_id="partial-1",
            order_id="exchange-1",
            symbol=SYMBOL,
            side="BUY",
            price=Decimal("125"),
            quantity=Decimal("2"),
            commission=Decimal("0"),
            commission_asset="USDT",
            fill_time=1_001,
            is_maker=False,
        ),
        campaign_id=CAMPAIGN,
    )

    assert strategy.pending_entry(SYMBOL) == client_order_id
    assert strategy.position(SYMBOL).quantity == Decimal("2")

    coordinator_instance.on_order_update(replace(wal_entry(), status="FILLED"))

    assert strategy.pending_entry(SYMBOL) is None


@pytest.mark.asyncio
async def test_execution_worker_processes_queued_entry_outside_enqueue_call():
    submitted = []
    submitted_event = asyncio.Event()

    async def submit(intent, **kwargs):
        submitted.append((intent, kwargs))
        submitted_event.set()
        return SimpleNamespace(status="NEW", payload={})

    executor = Mock()
    executor.submit = submit
    executor.resolve_recovered_unknowns_once = AsyncMock(return_value={})
    coordinator_instance = coordinator(executor=executor)
    worker_task = coordinator_instance.start()
    try:
        assert coordinator_instance.enqueue([entry_intent()], event_time=10) == 1
        await asyncio.wait_for(submitted_event.wait(), timeout=1)
        await coordinator_instance.execution_queue.join()
        assert len(submitted) == 1
        assert submitted[0][0].side == "BUY"
    finally:
        await coordinator_instance.stop()
        assert worker_task.done()


@pytest.mark.asyncio
async def test_coordinator_stop_is_idempotent():
    coordinator_instance = coordinator()

    await coordinator_instance.stop()
    await coordinator_instance.stop()


@pytest.mark.asyncio
async def test_stop_flushes_cancellations_when_worker_already_failed():
    account = account_mock()
    account.has_pending_cancellations = True

    async def flush_cancellations():
        account.has_pending_cancellations = False
        return ()

    account.flush_cancellations = flush_cancellations
    coordinator_instance = coordinator(account=account)

    async def fail():
        raise RuntimeError("worker failed")

    worker_task = asyncio.create_task(fail())
    await asyncio.gather(worker_task, return_exceptions=True)
    coordinator_instance.worker_task = worker_task

    with pytest.raises(RuntimeError, match="failed during queue drain"):
        await coordinator_instance.stop()

    assert account.has_pending_cancellations is False


@pytest.mark.asyncio
async def test_stop_waits_for_in_flight_entry_then_rescans_and_cancels_it():
    account = account_mock()
    submit_started = asyncio.Event()
    release_submit = asyncio.Event()
    active_order = SimpleNamespace(
        reduce_only=False,
        status="NEW",
        order_id="exchange-1",
        client_order_id=entry_intent().client_order_id,
        symbol=SYMBOL,
    )

    async def submit(_intent, **_kwargs):
        submit_started.set()
        await release_submit.wait()
        account.iter_orders.return_value = (active_order,)
        return replace(wal_entry(), status="NEW")

    def cancel_order(_order_id):
        account.has_pending_cancellations = True

    async def flush_cancellations():
        account.has_pending_cancellations = False
        return (active_order.client_order_id,)

    account.cancel_order = Mock(side_effect=cancel_order)
    account.flush_cancellations = AsyncMock(side_effect=flush_cancellations)
    executor = Mock(
        submit=submit,
        resolve_recovered_unknowns_once=AsyncMock(return_value={}),
    )
    coordinator_instance = coordinator(account=account, executor=executor)
    coordinator_instance.start()
    coordinator_instance.enqueue([entry_intent()], event_time=1)
    await submit_started.wait()

    stop_task = asyncio.create_task(coordinator_instance.stop())
    await asyncio.sleep(0)

    assert stop_task.done() is False
    assert coordinator_instance.worker_task is not None
    assert coordinator_instance.worker_task.cancelled() is False

    release_submit.set()
    await stop_task

    account.cancel_order.assert_called_once_with("exchange-1")
    account.flush_cancellations.assert_awaited_once_with()
    assert coordinator_instance.worker_task is None


@pytest.mark.asyncio
async def test_stop_recovers_and_cancels_submit_interrupted_at_timeout(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        execution_module, "_SUBMISSION_SETTLE_TIMEOUT_SECONDS", 0.01
    )
    submit_started = asyncio.Event()

    async def post_order(**_kwargs):
        submit_started.set()
        await asyncio.Event().wait()

    rest = Mock(
        post_order=post_order,
        query_order=AsyncMock(return_value={"orderId": 42, "status": "NEW"}),
        cancel_order=AsyncMock(return_value={"orderId": 42, "status": "CANCELED"}),
    )
    wal = OrderWAL(tmp_path / "long-breakout.jsonl")
    risk = RiskGuard(ACCOUNT_ID, RiskConfig())
    account = BinanceStrategyAccount(
        rest,
        wal,
        account_id=ACCOUNT_ID,
        strategy_id="long_breakout",
        risk_guard=risk,
        now_ms=iter(range(2_000, 2_100)).__next__,
    )
    executor = BinanceOrderExecutor(
        rest,
        wal,
        account_id=ACCOUNT_ID,
        risk_guard=risk,
        now_ms=iter(range(1_000, 1_100)).__next__,
    )
    strategy = LongBreakoutStrategy(symbol=SYMBOL)
    strategy.restore_pending_order(
        SYMBOL,
        client_order_id=entry_intent().client_order_id,
        campaign_id=CAMPAIGN,
        reduce_only=False,
    )
    coordinator_instance = coordinator(
        account=account,
        executor=executor,
        strategy=strategy,
    )
    coordinator_instance.start()
    coordinator_instance.enqueue([entry_intent()], event_time=1)
    await submit_started.wait()

    await coordinator_instance.stop()

    latest = wal.recover_latest()[entry_intent().client_order_id]
    assert latest.status == "CANCELLED"
    assert latest.payload["error"] == "submit_cancelled"
    rest.query_order.assert_awaited_once_with(
        SYMBOL, orig_client_order_id=entry_intent().client_order_id
    )
    rest.cancel_order.assert_awaited_once_with(
        SYMBOL, orig_client_order_id=entry_intent().client_order_id
    )
    assert strategy.pending_entry(SYMBOL) is None
    assert SYMBOL not in risk.blocked_symbols
