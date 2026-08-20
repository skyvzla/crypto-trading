import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, Mock

import pytest

from trading_platform.shared.events import Bar1s, OrderIntent
from trading_platform.shared.risk import RiskConfig, RiskGuard
from trading_platform.strategies.campaign_store import CampaignLease
from trading_platform.strategies.spike.execution_queue import ExecutionQueue
from trading_platform.strategies.spike.live import (
    CompositeEntryGate,
    SpikeExecutionCoordinator,
    SpikeLiveSettings,
)
from trading_platform.strategies.spike.main import SpikeLiveProcess
from trading_platform.strategies.universe import ExchangeSymbolSnapshot


def bar(symbol: str, sequence: int) -> Bar1s:
    event_time = sequence * 1_000
    return Bar1s(
        symbol=symbol,
        timestamp=event_time,
        available_time=event_time + 1_000,
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100"),
        volume=Decimal("1"),
        trade_count=1,
        vwap=Decimal("100"),
        first_aggregate_trade_id=sequence,
        last_aggregate_trade_id=sequence,
    )


def entry(symbol: str, signal_time: int) -> OrderIntent:
    return OrderIntent(
        symbol=symbol,
        side="SELL",
        price=Decimal("100"),
        quantity=Decimal("1"),
        client_order_id=f"spike_short_{symbol}_{signal_time}_tier1",
        reduce_only=False,
        strategy_id="spike_short",
        trigger_reason="spike_tier1",
    )


def exit_intent(symbol: str) -> OrderIntent:
    return OrderIntent(
        symbol=symbol,
        side="BUY",
        price=Decimal("99"),
        quantity=Decimal("1"),
        client_order_id=f"exit-{symbol}",
        order_type="MARKET",
        reduce_only=True,
        strategy_id="spike_short",
        trigger_reason="campaign_timeout_exit",
    )


class IntentStrategy:
    def __init__(self, intents_by_symbol):
        self.intents_by_symbol = intents_by_symbol
        self.strategies = {symbol: object() for symbol in intents_by_symbol}
        self.blocked_entry_symbols = frozenset()
        self.enabled = False

    def set_entry_enabled(self, enabled):
        self.enabled = enabled

    def is_symbol_entry_enabled(self, symbol):
        return symbol not in self.blocked_entry_symbols

    def on_bar1s(self, current):
        return list(self.intents_by_symbol[current.symbol])

    def drain_audit_events(self):
        return []


class LiveSignalIntentStrategy(IntentStrategy):
    def __init__(self, intents_by_symbol, expires_at_by_campaign):
        super().__init__(intents_by_symbol)
        self.expires_at_by_campaign = expires_at_by_campaign

    def campaign_entry_expire_time(self, campaign_id):
        return self.expires_at_by_campaign.get(campaign_id)


class MemoryCampaignStore:
    def __init__(self):
        self.active = None

    async def get_active(self):
        return self.active

    async def acquire(self, lease):
        if self.active is not None:
            return False
        self.active = lease
        return True

    async def release(self, campaign_id):
        return False


class ReleasingMemoryCampaignStore(MemoryCampaignStore):
    async def release(self, campaign_id):
        if self.active is None or self.active.campaign_id != campaign_id:
            return False
        self.active = None
        return True


def coordinator_for(strategy, executor, *, queue=None, account=None):
    gate = CompositeEntryGate(strategy)
    for name in ("execution", "market", "campaign"):
        gate.set_condition(name, True)
    if account is None:
        account = Mock(
            iter_orders=Mock(return_value=()),
            flush_cancellations=AsyncMock(return_value=()),
            has_pending_cancellations=False,
            has_open_position=Mock(return_value=False),
            all_orders_terminal=Mock(return_value=False),
        )
    coordinator = SpikeExecutionCoordinator(
        strategy=strategy,
        account=account,
        executor=executor,
        campaign_store=MemoryCampaignStore(),
        risk_guard=RiskGuard("spike-test", RiskConfig()),
        gate=gate,
        account_id="spike-test",
        execution_queue=queue,
    )
    return coordinator, gate


@pytest.mark.asyncio
async def test_slow_submit_does_not_block_strategy_event_ingestion_and_fifo_wins():
    release_submit = asyncio.Event()
    first_submit_started = asyncio.Event()
    submitted = []

    async def submit(intent, **_kwargs):
        submitted.append(intent.symbol)
        first_submit_started.set()
        await release_submit.wait()
        return Mock(status="NEW")

    strategy = IntentStrategy(
        {
            "BTCUSDT": [entry("BTCUSDT", 1_000)],
            "ETHUSDT": [entry("ETHUSDT", 2_000)],
        }
    )
    coordinator, _ = coordinator_for(
        strategy, Mock(submit=AsyncMock(side_effect=submit))
    )
    worker_task = coordinator.start_execution_worker()

    await coordinator.on_bar1s_queued(bar("BTCUSDT", 1))
    await asyncio.wait_for(first_submit_started.wait(), timeout=1)
    await asyncio.wait_for(
        coordinator.on_bar1s_queued(bar("ETHUSDT", 2)), timeout=0.1
    )

    assert submitted == ["BTCUSDT"]
    # 第二个同批信号在入队阶段即被首个 Campaign 排除，不能在首单释放后复活。
    assert coordinator.execution_queue.qsize == 0
    fifo_events = [
        event
        for event in coordinator._pending_audit_events
        if event.event_type.startswith("signal_")
    ]
    assert [event.event_type for event in fifo_events] == [
        "signal_acquired",
        "signal_skipped_overlap",
    ]
    assert [event.details["arrival_sequence"] for event in fifo_events] == [1, 2]

    release_submit.set()
    await asyncio.wait_for(coordinator.execution_queue.join(), timeout=1)
    assert submitted == ["BTCUSDT"]
    await coordinator.stop_execution_worker()
    assert worker_task.done()


@pytest.mark.asyncio
async def test_skipped_simultaneous_signal_cannot_reopen_after_fast_release():
    submitted = []

    async def submit(intent, **_kwargs):
        submitted.append(intent.symbol)
        return Mock(status="NEW")

    strategy = IntentStrategy(
        {
            "BTCUSDT": [entry("BTCUSDT", 1_000)],
            "ETHUSDT": [entry("ETHUSDT", 2_000)],
        }
    )
    account = Mock(
        iter_orders=Mock(return_value=()),
        flush_cancellations=AsyncMock(return_value=()),
        has_pending_cancellations=False,
        has_open_position=Mock(return_value=False),
        has_pending_position_update=Mock(return_value=False),
        all_orders_terminal=Mock(return_value=True),
    )
    coordinator, _ = coordinator_for(
        strategy,
        Mock(submit=AsyncMock(side_effect=submit)),
        account=account,
    )
    coordinator.campaign_store = ReleasingMemoryCampaignStore()

    await coordinator.on_bar1s_queued(bar("BTCUSDT", 1))
    await coordinator.on_bar1s_queued(bar("ETHUSDT", 2))
    coordinator.start_execution_worker()
    await asyncio.wait_for(coordinator.execution_queue.join(), timeout=1)

    assert submitted == ["BTCUSDT"]
    assert coordinator._owned_campaign_id is None
    assert coordinator.execution_queue.qsize == 0
    await coordinator.stop_execution_worker()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("expires_at", "expected_event"),
    [(2_500, "signal_skipped_stale"), (None, "signal_skipped_invalid")],
)
async def test_queued_entry_is_rejected_when_signal_expires_or_is_invalidated(
    expires_at, expected_event
):
    campaign_id = "spike_short:BTCUSDT:1000"
    strategy = LiveSignalIntentStrategy(
        {"BTCUSDT": [entry("BTCUSDT", 1_000)]},
        {campaign_id: expires_at} if expires_at is not None else {},
    )
    executor = Mock(submit=AsyncMock(return_value=Mock(status="NEW")))
    coordinator, _ = coordinator_for(strategy, executor)
    coordinator._now_ms = lambda: 3_000

    await coordinator.on_bar1s_queued(bar("BTCUSDT", 1))
    coordinator.start_execution_worker()
    await asyncio.wait_for(coordinator.execution_queue.join(), timeout=1)

    executor.submit.assert_not_awaited()
    assert coordinator.campaign_store.active is None
    assert coordinator._signal_arbiter.active_campaign_id is None
    assert coordinator._pending_audit_events[-1].event_type == expected_event
    await coordinator.stop_execution_worker()


@pytest.mark.asyncio
async def test_queued_entry_uses_remaining_signal_ttl_at_submit_time():
    campaign_id = "spike_short:BTCUSDT:1000"
    strategy = LiveSignalIntentStrategy(
        {"BTCUSDT": [entry("BTCUSDT", 1_000)]},
        {campaign_id: 5_000},
    )
    executor = Mock(submit=AsyncMock(return_value=Mock(status="NEW")))
    coordinator, _ = coordinator_for(strategy, executor)
    coordinator._now_ms = lambda: 3_000

    await coordinator.on_bar1s_queued(bar("BTCUSDT", 1))
    coordinator.start_execution_worker()
    await asyncio.wait_for(coordinator.execution_queue.join(), timeout=1)

    submitted = executor.submit.await_args.args[0]
    assert submitted.ttl_ms == 2_000
    await coordinator.stop_execution_worker()


@pytest.mark.asyncio
async def test_execution_worker_submits_exit_before_earlier_entry():
    submitted = []

    async def submit(intent, **_kwargs):
        submitted.append(intent.client_order_id)
        return Mock(status="NEW")

    queued_entry = entry("BTCUSDT", 1_000)
    queued_exit = exit_intent("BTCUSDT")
    strategy = IntentStrategy(
        {"BTCUSDT": [queued_entry], "ETHUSDT": [queued_exit]}
    )
    coordinator, _ = coordinator_for(
        strategy, Mock(submit=AsyncMock(side_effect=submit))
    )
    lease = CampaignLease(
        "spike_short:BTCUSDT:1000", "spike_short", "BTCUSDT", 1_000
    )
    coordinator._owned_campaign_id = lease.campaign_id
    coordinator._owned_campaign_lease = lease
    coordinator.campaign_store.active = lease

    await coordinator.on_bar1s_queued(bar("BTCUSDT", 1))
    await coordinator.on_bar1s_queued(bar("ETHUSDT", 2))
    coordinator.start_execution_worker()
    await asyncio.wait_for(coordinator.execution_queue.join(), timeout=1)

    assert submitted == [queued_exit.client_order_id, queued_entry.client_order_id]
    await coordinator.stop_execution_worker()


@pytest.mark.asyncio
async def test_full_entry_execution_queue_closes_entries_but_keeps_exit():
    open_entry = Mock(
        reduce_only=False,
        status="NEW",
        order_id="entry-order",
        symbol="BTCUSDT",
    )
    account = Mock(
        iter_orders=Mock(return_value=(open_entry,)),
        cancel_order=Mock(return_value=True),
        flush_cancellations=AsyncMock(return_value=()),
        has_pending_cancellations=True,
        has_open_position=Mock(return_value=True),
        all_orders_terminal=Mock(return_value=False),
    )
    strategy = IntentStrategy(
        {
            "BTCUSDT": [entry("BTCUSDT", 1_000)],
            "ETHUSDT": [entry("ETHUSDT", 2_000)],
            "BNBUSDT": [exit_intent("BNBUSDT")],
        }
    )
    queue = ExecutionQueue(max_pending_entries=1)
    queued_entry = entry("BTCUSDT", 500)
    queue.put_nowait("entry", intent=queued_entry, event_time=500)
    coordinator, gate = coordinator_for(
        strategy,
        Mock(submit=AsyncMock(return_value=Mock(status="NEW"))),
        queue=queue,
        account=account,
    )

    await coordinator.on_bar1s_queued(bar("BTCUSDT", 1))
    await coordinator.on_bar1s_queued(bar("ETHUSDT", 2))
    await coordinator.on_bar1s_queued(bar("BNBUSDT", 3))

    assert gate.condition("event_queue") is False
    account.cancel_order.assert_called_once_with("entry-order")
    jobs = [await coordinator.execution_queue.get() for _ in range(3)]
    assert [job.kind for job in jobs] == ["exit", "cancel", "entry"]
    assert jobs[-1].intent == queued_entry
    for _ in jobs:
        coordinator.execution_queue.task_done()


@pytest.mark.asyncio
async def test_live_cancellation_is_queued_behind_exit_and_ahead_of_entry():
    open_entry = Mock(
        reduce_only=False,
        status="NEW",
        order_id="entry-order",
        symbol="BTCUSDT",
    )
    account = Mock(
        iter_orders=Mock(return_value=(open_entry,)),
        cancel_order=Mock(return_value=True),
        flush_cancellations=AsyncMock(return_value=("entry-order",)),
        has_pending_cancellations=True,
        has_open_position=Mock(return_value=True),
        all_orders_terminal=Mock(return_value=False),
    )
    strategy = IntentStrategy({"BTCUSDT": []})
    coordinator, _ = coordinator_for(
        strategy,
        Mock(submit=AsyncMock(return_value=Mock(status="NEW"))),
        account=account,
    )
    coordinator._execution_worker_running = True
    coordinator.execution_queue.put_nowait(
        "entry", intent=entry("BTCUSDT", 1_000), event_time=1_000
    )
    coordinator.execution_queue.put_nowait(
        "exit", intent=exit_intent("BTCUSDT"), event_time=2_000
    )

    await coordinator.cancel_open_entry_orders()

    account.flush_cancellations.assert_not_awaited()
    jobs = [await coordinator.execution_queue.get() for _ in range(3)]
    assert [job.kind for job in jobs] == ["exit", "cancel", "entry"]
    for _ in jobs:
        coordinator.execution_queue.task_done()


def make_process(symbols=("BTCUSDT",)):
    process = SpikeLiveProcess(
        SpikeLiveSettings(
            account_id="spike-test", symbols=list(symbols), total_notional="20"
        ),
        binance=Mock(),
        database=Mock(),
        redis_config=Mock(),
        strategy_config=Mock(account_id="spike-test"),
    )
    strategy = IntentStrategy({symbol: [] for symbol in symbols})
    process.gate = CompositeEntryGate(strategy)
    process.gate.set_condition("event_queue", True)
    process.exchange_symbol_snapshot = ExchangeSymbolSnapshot(
        allowed_symbols=frozenset(symbols),
        blocked_symbols=frozenset(),
        blocked_reasons={},
    )
    process.coordinator = Mock(close_entry_pipeline=Mock())
    return process


@pytest.mark.asyncio
async def test_full_market_event_queue_fails_closed_without_dropping_current_bar():
    process = make_process()
    process._market_events = asyncio.Queue(maxsize=1)
    first = bar("BTCUSDT", 1)
    second = bar("BTCUSDT", 2)
    process._market_events.put_nowait(first)

    blocked_put = asyncio.create_task(process._enqueue_market_event(second))
    await asyncio.sleep(0)

    assert process._market_event_queue_overflowed is True
    assert process.gate.condition("event_queue") is False
    process.coordinator.close_entry_pipeline.assert_called_once()
    assert await process._market_events.get() == first
    process._market_events.task_done()
    await asyncio.wait_for(blocked_put, timeout=1)
    assert await process._market_events.get() == second
    process._market_events.task_done()


@pytest.mark.asyncio
async def test_pubsub_listener_keeps_accepting_while_strategy_loop_is_slow():
    process = make_process()
    process._queued_execution_started = True
    first_strategy_call = asyncio.Event()
    release_strategy = asyncio.Event()

    async def slow_on_bar(_bar):
        first_strategy_call.set()
        await release_strategy.wait()

    process.coordinator.on_bar1s_queued = AsyncMock(side_effect=slow_on_bar)
    process.coordinator.cancel_open_entry_orders = AsyncMock()
    messages = [
        {"type": "message", "data": bar("BTCUSDT", 1).to_json()},
        {"type": "message", "data": bar("BTCUSDT", 2).to_json()},
    ]

    class PubSub:
        async def subscribe(self, *_channels):
            return None

        async def listen(self):
            for message in messages:
                yield message

        async def aclose(self):
            return None

    process.redis = Mock(pubsub=Mock(return_value=PubSub()))
    strategy_task = asyncio.create_task(process._strategy_event_loop())
    listener_task = asyncio.create_task(process._bar_loop())

    await asyncio.wait_for(first_strategy_call.wait(), timeout=1)
    await asyncio.wait_for(listener_task, timeout=0.1)
    assert process._market_events.qsize() == 1

    release_strategy.set()
    await asyncio.wait_for(process._market_events.join(), timeout=1)
    strategy_task.cancel()
    await asyncio.gather(strategy_task, return_exceptions=True)
