import asyncio
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from trading_platform.shared.binance.live_executor import BinanceOrderExecutor
from trading_platform.shared.events import Bar1s, OrderIntent
from trading_platform.shared.execution_recovery import OrderWAL
from trading_platform.shared.risk import RiskConfig, RiskGuard
from trading_platform.strategies.campaign_store import CampaignLease
from trading_platform.strategies.spike.execution_queue import ExecutionJob, ExecutionQueue
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


class CollectingJournal:
    def __init__(self):
        self.events = []

    async def append(self, event_type, **kwargs):
        event_id = f"journal-{len(self.events) + 1}"
        values = {
            **kwargs,
            "event_id": event_id,
            "trace_id": kwargs.get("trace_id") or event_id,
            "event_type": event_type,
        }
        event = SimpleNamespace(**values)
        self.events.append(event)
        return event


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


class BlockingSnapshotRuntime:
    def __init__(self) -> None:
        self.failed = False
        self.fault_reason = None
        self.observe_started = asyncio.Event()
        self.release_first_observe = asyncio.Event()
        self.started_timestamps = []
        self.completed_timestamps = []
        self.snapshot_campaigns = []

    async def observe_bar(self, current):
        self.started_timestamps.append(current.timestamp)
        self.observe_started.set()
        if len(self.started_timestamps) == 1:
            await self.release_first_observe.wait()
        self.completed_timestamps.append(current.timestamp)
        return ()

    async def ensure_signal_snapshot(
        self, *, campaign_id, symbol, signal_time_ms, metadata=None
    ):
        assert signal_time_ms in self.completed_timestamps
        self.snapshot_campaigns.append(campaign_id)
        return f"snapshot:{campaign_id}"


def coordinator_for(
    strategy,
    executor,
    *,
    queue=None,
    account=None,
    event_journal=None,
    snapshot_runtime=None,
):
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
        event_journal=event_journal,
        snapshot_runtime=snapshot_runtime,
    )
    return coordinator, gate


def own_campaign(coordinator, campaign_id, symbol, started_at_ms):
    lease = CampaignLease(
        campaign_id, "spike_short", symbol, started_at_ms
    )
    coordinator._owned_campaign_id = campaign_id
    coordinator._owned_campaign_lease = lease
    coordinator.campaign_store.active = lease


@pytest.mark.asyncio
async def test_queued_exit_precedes_entry_while_snapshot_observe_is_blocked():
    current_entry = entry("BTCUSDT", 1_000)
    current_exit = exit_intent("BTCUSDT")
    snapshot_runtime = BlockingSnapshotRuntime()
    submitted = []
    exit_submitted = asyncio.Event()

    async def submit(intent, **_kwargs):
        submitted.append(intent.client_order_id)
        if intent.reduce_only:
            exit_submitted.set()
        return Mock(status="NEW")

    coordinator, _ = coordinator_for(
        IntentStrategy({"BTCUSDT": [current_entry, current_exit]}),
        Mock(submit=AsyncMock(side_effect=submit)),
        snapshot_runtime=snapshot_runtime,
    )
    own_campaign(
        coordinator, "spike_short:BTCUSDT:1000", "BTCUSDT", 1_000
    )
    coordinator.start_execution_worker()

    await asyncio.wait_for(coordinator.on_bar1s_queued(bar("BTCUSDT", 1)), 0.1)
    await asyncio.wait_for(exit_submitted.wait(), 1)
    assert submitted == [current_exit.client_order_id]
    assert snapshot_runtime.snapshot_campaigns == []

    snapshot_runtime.release_first_observe.set()
    await asyncio.wait_for(
        asyncio.gather(*tuple(coordinator._entry_snapshot_tasks)), 1
    )
    await asyncio.wait_for(coordinator.execution_queue.join(), 1)
    assert submitted == [current_exit.client_order_id, current_entry.client_order_id]
    assert snapshot_runtime.snapshot_campaigns == ["spike_short:BTCUSDT:1000"]
    await coordinator.stop()


@pytest.mark.asyncio
async def test_direct_exit_precedes_entry_while_snapshot_observe_is_blocked():
    current_entry = entry("BTCUSDT", 1_000)
    current_exit = exit_intent("BTCUSDT")
    snapshot_runtime = BlockingSnapshotRuntime()
    submitted = []

    async def submit(intent, **_kwargs):
        submitted.append(intent.client_order_id)
        return Mock(status="NEW")

    coordinator, _ = coordinator_for(
        IntentStrategy({"BTCUSDT": [current_entry, current_exit]}),
        Mock(submit=AsyncMock(side_effect=submit)),
        snapshot_runtime=snapshot_runtime,
    )
    own_campaign(
        coordinator, "spike_short:BTCUSDT:1000", "BTCUSDT", 1_000
    )

    await asyncio.wait_for(coordinator.on_bar1s(bar("BTCUSDT", 1)), 0.1)
    assert submitted == [current_exit.client_order_id]
    assert snapshot_runtime.snapshot_campaigns == []

    snapshot_runtime.release_first_observe.set()
    await asyncio.wait_for(
        asyncio.gather(*tuple(coordinator._entry_snapshot_tasks)), 1
    )
    assert submitted == [current_exit.client_order_id, current_entry.client_order_id]
    assert snapshot_runtime.snapshot_campaigns == ["spike_short:BTCUSDT:1000"]
    await coordinator.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("expires_at", [2_500, None])
async def test_direct_entry_is_rechecked_after_snapshot_delay(
    expires_at,
):
    campaign_id = "spike_short:BTCUSDT:1000"
    strategy = LiveSignalIntentStrategy(
        {"BTCUSDT": [entry("BTCUSDT", 1_000)]},
        {campaign_id: expires_at} if expires_at is not None else {},
    )
    snapshot_runtime = BlockingSnapshotRuntime()
    executor = Mock(submit=AsyncMock(return_value=Mock(status="NEW")))
    coordinator, _ = coordinator_for(
        strategy,
        executor,
        snapshot_runtime=snapshot_runtime,
    )
    now_ms = [2_000]
    coordinator._now_ms = lambda: now_ms[0]

    await asyncio.wait_for(coordinator.on_bar1s(bar("BTCUSDT", 1)), 0.1)
    await asyncio.wait_for(snapshot_runtime.observe_started.wait(), 1)
    now_ms[0] = 3_000
    snapshot_runtime.release_first_observe.set()
    await asyncio.wait_for(
        asyncio.gather(*tuple(coordinator._entry_snapshot_tasks)), 1
    )

    executor.submit.assert_not_awaited()
    assert coordinator._signal_arbiter.active_campaign_id is None
    assert coordinator._pending_audit_events[-1].event_type == (
        "signal_skipped_stale" if expires_at is not None else "signal_skipped_invalid"
    )
    await coordinator.stop()


@pytest.mark.asyncio
async def test_direct_entry_with_delayed_snapshot_submits_when_still_valid():
    campaign_id = "spike_short:BTCUSDT:1000"
    strategy = LiveSignalIntentStrategy(
        {"BTCUSDT": [entry("BTCUSDT", 1_000)]},
        {campaign_id: 5_000},
    )
    snapshot_runtime = BlockingSnapshotRuntime()
    executor = Mock(submit=AsyncMock(return_value=Mock(status="NEW")))
    coordinator, _ = coordinator_for(
        strategy,
        executor,
        snapshot_runtime=snapshot_runtime,
    )
    now_ms = [2_000]
    coordinator._now_ms = lambda: now_ms[0]

    await asyncio.wait_for(coordinator.on_bar1s(bar("BTCUSDT", 1)), 0.1)
    assert coordinator._signal_arbiter.active_campaign_id == campaign_id
    await asyncio.wait_for(snapshot_runtime.observe_started.wait(), 1)

    now_ms[0] = 3_000
    snapshot_runtime.release_first_observe.set()
    await asyncio.wait_for(
        asyncio.gather(*tuple(coordinator._entry_snapshot_tasks)), 1
    )

    executor.submit.assert_awaited_once()
    assert executor.submit.await_args.args[0].client_order_id == (
        "spike_short_BTCUSDT_1000_tier1"
    )
    assert snapshot_runtime.snapshot_campaigns == [campaign_id]
    assert coordinator._signal_arbiter.active_campaign_id == campaign_id
    await coordinator.stop()


@pytest.mark.asyncio
async def test_snapshot_observations_and_entry_arbitration_preserve_bar_fifo():
    snapshot_runtime = BlockingSnapshotRuntime()
    coordinator, _ = coordinator_for(
        IntentStrategy(
            {
                "BTCUSDT": [entry("BTCUSDT", 1_000)],
                "ETHUSDT": [entry("ETHUSDT", 2_000)],
            }
        ),
        Mock(submit=AsyncMock(return_value=Mock(status="NEW"))),
        snapshot_runtime=snapshot_runtime,
    )

    await asyncio.wait_for(coordinator.on_bar1s_queued(bar("BTCUSDT", 1)), 0.1)
    await asyncio.wait_for(snapshot_runtime.observe_started.wait(), 1)
    await asyncio.wait_for(coordinator.on_bar1s_queued(bar("ETHUSDT", 2)), 0.1)
    await asyncio.sleep(0)
    assert snapshot_runtime.started_timestamps == [1_000]
    assert coordinator.execution_queue.qsize == 0

    snapshot_runtime.release_first_observe.set()
    await asyncio.wait_for(
        asyncio.gather(*tuple(coordinator._entry_snapshot_tasks)), 1
    )
    assert snapshot_runtime.started_timestamps == [1_000, 2_000]
    assert snapshot_runtime.completed_timestamps == [1_000, 2_000]
    assert snapshot_runtime.snapshot_campaigns == [
        "spike_short:BTCUSDT:1000",
        "spike_short:ETHUSDT:2000",
    ]
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
    assert coordinator.execution_queue.qsize == 1
    await coordinator.stop()


@pytest.mark.asyncio
async def test_stop_waits_for_inflight_snapshot_observation():
    snapshot_runtime = BlockingSnapshotRuntime()
    coordinator, _ = coordinator_for(
        IntentStrategy({"BTCUSDT": []}),
        Mock(submit=AsyncMock()),
        snapshot_runtime=snapshot_runtime,
    )

    await asyncio.wait_for(coordinator.on_bar1s_queued(bar("BTCUSDT", 1)), 0.1)
    await asyncio.wait_for(snapshot_runtime.observe_started.wait(), 1)
    stop_task = asyncio.create_task(coordinator.stop())
    await asyncio.sleep(0.02)
    assert not stop_task.done()

    snapshot_runtime.release_first_observe.set()
    await asyncio.wait_for(stop_task, 1)
    await coordinator.on_bar1s_queued(bar("BTCUSDT", 2))
    assert snapshot_runtime.started_timestamps == [1_000]


@pytest.mark.asyncio
@pytest.mark.parametrize("handler_name", ["on_bar1s", "on_bar1s_queued"])
async def test_stop_wins_lock_race_and_rejects_later_entry_bar(handler_name):
    snapshot_runtime = BlockingSnapshotRuntime()
    strategy = IntentStrategy(
        {
            "BTCUSDT": [],
            "ETHUSDT": [entry("ETHUSDT", 2_000)],
        }
    )
    strategy.on_bar1s = Mock(wraps=strategy.on_bar1s)
    coordinator, _ = coordinator_for(
        strategy,
        Mock(submit=AsyncMock(return_value=Mock(status="NEW"))),
        snapshot_runtime=snapshot_runtime,
    )
    handler = getattr(coordinator, handler_name)

    await asyncio.wait_for(handler(bar("BTCUSDT", 1)), 0.1)
    await asyncio.wait_for(snapshot_runtime.observe_started.wait(), 1)
    await coordinator._lock.acquire()
    stop_task = asyncio.create_task(coordinator.stop())
    await asyncio.sleep(0)
    later_bar_task = asyncio.create_task(handler(bar("ETHUSDT", 2)))
    await asyncio.sleep(0)
    coordinator._lock.release()

    await asyncio.wait_for(later_bar_task, 0.1)
    assert strategy.on_bar1s.call_count == 1
    assert snapshot_runtime.started_timestamps == [1_000]
    assert snapshot_runtime.snapshot_campaigns == []
    assert coordinator.execution_queue.qsize == 0
    assert not stop_task.done()

    snapshot_runtime.release_first_observe.set()
    await asyncio.wait_for(stop_task, 1)


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
@pytest.mark.parametrize("invalidator", ["gate", "symbol"])
async def test_queued_entry_invalidated_before_execution_releases_local_fifo(
    invalidator,
):
    first_campaign = "spike_short:BTCUSDT:1000"
    second_campaign = "spike_short:ETHUSDT:2000"
    strategy = LiveSignalIntentStrategy(
        {
            "BTCUSDT": [entry("BTCUSDT", 1_000)],
            "ETHUSDT": [entry("ETHUSDT", 2_000)],
        },
        {first_campaign: 10_000, second_campaign: 10_000},
    )
    executor = Mock(submit=AsyncMock(return_value=Mock(status="NEW")))
    coordinator, gate = coordinator_for(strategy, executor)
    coordinator._now_ms = lambda: 3_000

    await coordinator.on_bar1s_queued(bar("BTCUSDT", 1))
    if invalidator == "gate":
        gate.set_condition("market", False)
    else:
        strategy.blocked_entry_symbols = frozenset({"BTCUSDT"})
    coordinator.start_execution_worker()
    await asyncio.wait_for(coordinator.execution_queue.join(), timeout=1)

    executor.submit.assert_not_awaited()
    assert coordinator.campaign_store.active is None
    assert coordinator._signal_arbiter.active_campaign_id is None
    skipped = coordinator._pending_audit_events[-1]
    assert skipped.event_type == "signal_skipped_invalid"
    assert skipped.details == {
        "stage": "execution_queue",
        "reason": "entry_gate_closed" if invalidator == "gate" else "symbol_blocked",
    }

    if invalidator == "gate":
        gate.set_condition("market", True)
    else:
        strategy.blocked_entry_symbols = frozenset()
    await coordinator.on_bar1s_queued(bar("ETHUSDT", 2))
    await asyncio.wait_for(coordinator.execution_queue.join(), timeout=1)

    executor.submit.assert_awaited_once()
    assert executor.submit.await_args.args[0].symbol == "ETHUSDT"
    await coordinator.stop_execution_worker()


@pytest.mark.asyncio
async def test_queued_entry_noop_never_releases_an_owned_redis_campaign():
    campaign_id = "spike_short:BTCUSDT:1000"
    strategy = LiveSignalIntentStrategy(
        {"BTCUSDT": [entry("BTCUSDT", 1_000)]},
        {campaign_id: 10_000},
    )
    executor = Mock(submit=AsyncMock(return_value=Mock(status="NEW")))
    coordinator, gate = coordinator_for(strategy, executor)
    coordinator._now_ms = lambda: 3_000

    await coordinator.on_bar1s_queued(bar("BTCUSDT", 1))
    assert await coordinator._acquire_campaign(campaign_id, "BTCUSDT", 2_000)
    gate.set_condition("market", False)
    coordinator.start_execution_worker()
    await asyncio.wait_for(coordinator.execution_queue.join(), timeout=1)

    executor.submit.assert_not_awaited()
    assert coordinator.campaign_store.active is not None
    assert coordinator._owned_campaign_id == campaign_id
    assert coordinator._signal_arbiter.active_campaign_id == campaign_id
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
    journal = CollectingJournal()
    queue = ExecutionQueue(max_pending_entries=1)
    queued_entry = entry("BTCUSDT", 500)
    queue.put_nowait("entry", intent=queued_entry, event_time=500)
    coordinator, gate = coordinator_for(
        strategy,
        Mock(submit=AsyncMock(return_value=Mock(status="NEW"))),
        queue=queue,
        account=account,
        event_journal=journal,
    )

    await coordinator.on_bar1s_queued(bar("BTCUSDT", 1))
    await coordinator.on_bar1s_queued(bar("ETHUSDT", 2))
    await coordinator.on_bar1s_queued(bar("BNBUSDT", 3))

    assert gate.condition("event_queue") is False
    account.cancel_order.assert_called_once_with("entry-order")
    jobs = [await coordinator.execution_queue.get() for _ in range(3)]
    assert [job.kind for job in jobs] == ["exit", "cancel", "entry"]
    assert jobs[-1].intent == queued_entry
    queued_events = [
        event
        for event in journal.events
        if event.event_type == "execution.intent_queued"
    ]
    rejected_events = [
        event
        for event in journal.events
        if event.event_type == "execution.intent_rejected"
    ]
    assert [event.client_order_id for event in queued_events] == [
        "exit-BNBUSDT"
    ]
    assert {
        event.client_order_id for event in rejected_events
    } == {"spike_short_BTCUSDT_1000_tier1", "spike_short_ETHUSDT_2000_tier1"}
    for _ in jobs:
        coordinator.execution_queue.task_done()


@pytest.mark.asyncio
async def test_journal_records_market_input_before_strategy_and_intent_before_worker():
    journal = CollectingJournal()
    order_intent = entry("BTCUSDT", 1_000)

    class OrderingStrategy(IntentStrategy):
        def on_bar1s(self, current):
            assert [item.event_type for item in journal.events] == [
                "market.bar1s_received"
            ]
            return super().on_bar1s(current)

    strategy = OrderingStrategy({"BTCUSDT": [order_intent]})

    async def submit(intent, **_kwargs):
        assert journal.events[-1].event_type == "execution.order_submit_started"
        return SimpleNamespace(
            status="NEW",
            client_order_id=intent.client_order_id,
            exchange_order_id="12345",
            payload={"status": "NEW"},
        )

    coordinator, _ = coordinator_for(
        strategy,
        Mock(submit=AsyncMock(side_effect=submit)),
        event_journal=journal,
    )
    current = bar("BTCUSDT", 1)

    await coordinator.on_bar1s_queued(current)

    assert [item.event_type for item in journal.events] == [
        "market.bar1s_received",
        "market.bar1s_processed",
        "execution.intent_queued",
        "strategy.audit.signal_acquired",
    ]
    assert journal.events[0].details == current.to_dict()
    assert journal.events[1].details == {"intent_count": 1}
    assert journal.events[2].details["intent"]["client_order_id"] == (
        order_intent.client_order_id
    )

    worker = coordinator.start_execution_worker()
    await asyncio.wait_for(coordinator.execution_queue.join(), timeout=1)
    assert [item.event_type for item in journal.events][-7:] == [
        "execution.job_started",
        "campaign.acquire_started",
        "campaign.acquire_result",
        "strategy.audit.campaign_acquired",
        "execution.order_submit_started",
        "execution.order_submit_result",
        "execution.job_completed",
    ]
    assert journal.events[-1].details["queue_sequence"] == 1
    await coordinator.stop_execution_worker()
    assert worker.done()


@pytest.mark.asyncio
async def test_worker_failure_is_journaled_and_closes_event_queue_gate():
    journal = CollectingJournal()
    strategy = IntentStrategy({"BTCUSDT": [entry("BTCUSDT", 1_000)]})
    coordinator, gate = coordinator_for(
        strategy,
        Mock(submit=AsyncMock(side_effect=RuntimeError("exchange unavailable"))),
        event_journal=journal,
    )

    await coordinator.on_bar1s_queued(bar("BTCUSDT", 1))
    worker = coordinator.start_execution_worker()
    await asyncio.wait_for(coordinator.execution_queue.join(), timeout=1)

    assert gate.condition("event_queue") is False
    assert journal.events[-1].event_type == "execution.job_failed"
    assert journal.events[-1].severity == "error"
    assert journal.events[-1].details["error_type"] == "RuntimeError"
    assert journal.events[-1].details["error_message"] == "exchange unavailable"
    with pytest.raises(RuntimeError, match="exchange unavailable"):
        await worker


@pytest.mark.asyncio
async def test_worker_failure_journal_error_does_not_replace_execution_error():
    class FailingJobJournal(CollectingJournal):
        async def append(self, event_type, **kwargs):
            if event_type == "execution.job_failed":
                raise OSError("journal disk full")
            return await super().append(event_type, **kwargs)

    journal = FailingJobJournal()
    strategy = IntentStrategy({"BTCUSDT": [entry("BTCUSDT", 1_000)]})
    coordinator, gate = coordinator_for(
        strategy,
        Mock(submit=AsyncMock(side_effect=RuntimeError("exchange unavailable"))),
        event_journal=journal,
    )

    await coordinator.on_bar1s_queued(bar("BTCUSDT", 1))
    worker = coordinator.start_execution_worker()
    await asyncio.wait_for(coordinator.execution_queue.join(), timeout=1)

    assert gate.condition("event_queue") is False
    with pytest.raises(RuntimeError, match="exchange unavailable") as raised:
        await worker
    notes = "\n".join(raised.value.__notes__ or ())
    assert "execution.job_failed journal append failed" in notes
    assert "OSError: journal disk full" in notes


@pytest.mark.asyncio
async def test_worker_cancellation_journal_error_does_not_replace_cancelled_error():
    class FailingCancellationJournal(CollectingJournal):
        async def append(self, event_type, **kwargs):
            if event_type == "execution.job_cancelled":
                raise OSError("journal disk full")
            return await super().append(event_type, **kwargs)

    journal = FailingCancellationJournal()
    strategy = IntentStrategy({"BTCUSDT": []})
    coordinator, _ = coordinator_for(
        strategy,
        Mock(),
        event_journal=journal,
    )
    coordinator._flush_cancellations = AsyncMock(side_effect=asyncio.CancelledError())
    job = ExecutionJob(priority=1, sequence=1, kind="cancel", event_time=1_000)

    with pytest.raises(asyncio.CancelledError) as raised:
        await coordinator._handle_execution_job(job)

    notes = "\n".join(raised.value.__notes__ or ())
    assert "execution.job_cancelled journal append failed" in notes
    assert "OSError: journal disk full" in notes


@pytest.mark.asyncio
async def test_order_submit_cancellation_after_wal_fsync_is_journaled(tmp_path):
    submit_started = asyncio.Event()
    release_submit = asyncio.Event()

    async def post_order(**_kwargs):
        submit_started.set()
        await release_submit.wait()
        return {"status": "NEW", "orderId": 42}

    wal_path = tmp_path / "orders.jsonl"
    executor = BinanceOrderExecutor(
        Mock(post_order=AsyncMock(side_effect=post_order), query_order=AsyncMock()),
        OrderWAL(wal_path),
        account_id="spike-test",
        now_ms=lambda: 1_000,
    )
    journal = CollectingJournal()
    coordinator, _ = coordinator_for(
        IntentStrategy({"BTCUSDT": []}), executor, event_journal=journal
    )
    intent = entry("BTCUSDT", 1_000)
    intent.campaign_id = "spike_short:BTCUSDT:1000"
    coordinator._owned_campaign_id = intent.campaign_id

    task = asyncio.create_task(coordinator._submit(intent))
    await asyncio.wait_for(submit_started.wait(), timeout=1)
    assert wal_path.read_text().splitlines()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert [event.event_type for event in journal.events] == [
        "execution.order_submit_started",
        "execution.order_submit_cancelled",
    ]
    assert journal.events[-1].details["error_type"] == "CancelledError"


@pytest.mark.asyncio
async def test_order_submit_failure_is_journaled_without_replacing_original_error():
    journal = CollectingJournal()
    coordinator, _ = coordinator_for(
        IntentStrategy({"BTCUSDT": []}),
        Mock(submit=AsyncMock(side_effect=RuntimeError("exchange unavailable"))),
        event_journal=journal,
    )
    intent = entry("BTCUSDT", 1_000)
    intent.campaign_id = "spike_short:BTCUSDT:1000"
    coordinator._owned_campaign_id = intent.campaign_id

    with pytest.raises(RuntimeError, match="exchange unavailable"):
        await coordinator._submit(intent)

    assert [event.event_type for event in journal.events] == [
        "execution.order_submit_started",
        "execution.order_submit_failed",
    ]
    assert journal.events[-1].details["error_type"] == "RuntimeError"


@pytest.mark.asyncio
async def test_order_submit_cancel_journal_failure_keeps_cancelled_error():
    class FailingSubmitCancellationJournal(CollectingJournal):
        async def append(self, event_type, **kwargs):
            if event_type == "execution.order_submit_cancelled":
                raise OSError("journal disk full")
            return await super().append(event_type, **kwargs)

    journal = FailingSubmitCancellationJournal()
    coordinator, _ = coordinator_for(
        IntentStrategy({"BTCUSDT": []}),
        Mock(submit=AsyncMock(side_effect=asyncio.CancelledError())),
        event_journal=journal,
    )
    intent = entry("BTCUSDT", 1_000)
    intent.campaign_id = "spike_short:BTCUSDT:1000"
    coordinator._owned_campaign_id = intent.campaign_id

    with pytest.raises(asyncio.CancelledError) as raised:
        await coordinator._submit(intent)

    notes = "\n".join(raised.value.__notes__ or ())
    assert "execution.order_submit_cancelled journal append failed" in notes
    assert "OSError: journal disk full" in notes


@pytest.mark.asyncio
async def test_journal_failure_propagates_fail_closed_before_strategy_runs():
    strategy = IntentStrategy({"BTCUSDT": [entry("BTCUSDT", 1_000)]})
    strategy.on_bar1s = Mock(wraps=strategy.on_bar1s)
    journal = Mock(append=AsyncMock(side_effect=OSError("journal disk full")))
    coordinator, gate = coordinator_for(
        strategy,
        Mock(submit=AsyncMock()),
        event_journal=journal,
    )

    with pytest.raises(OSError, match="journal disk full"):
        await coordinator.on_bar1s_queued(bar("BTCUSDT", 1))

    strategy.on_bar1s.assert_not_called()
    assert gate.condition("execution") is False
    assert coordinator.risk_guard.halted is True
    assert coordinator.risk_guard.halt_reason == "execution event journal write failed"


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
