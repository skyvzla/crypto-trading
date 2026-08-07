from dataclasses import dataclass
from decimal import Decimal

import pytest

from trading_platform.shared.events import Bar1s, Kline, OrderIntent, Position
from trading_platform.shared.risk import RiskConfig, RiskGuard
from trading_platform.strategies.campaign_store import CampaignLease
from trading_platform.strategies.spike_live import (
    CompositeEntryGate,
    SpikeExecutionCoordinator,
)
from trading_platform.strategies.spike_short import DynamicSpikeBacktestStrategy


MINUTE = 60_000


def _kline(interval: str, open_time: int, duration: int) -> Kline:
    return Kline(
        symbol="BTCUSDT",
        interval=interval,
        open_time=open_time,
        close_time=open_time + duration - 1,
        available_time=open_time + duration,
        open=Decimal("100"),
        high=Decimal("102"),
        low=Decimal("80") if interval == "1m" else Decimal("98"),
        close=Decimal("100"),
        volume=Decimal("10"),
    )


def _bar(timestamp: int, close: str, high: str, volume: str = "1") -> Bar1s:
    price = Decimal(close)
    return Bar1s(
        symbol="BTCUSDT",
        timestamp=timestamp,
        available_time=timestamp + 1_000,
        open=price,
        high=Decimal(high),
        low=min(price, Decimal("99")),
        close=price,
        volume=Decimal(volume),
        trade_count=1,
        vwap=price,
    )


def _entry_events() -> list[Bar1s | Kline]:
    minute_start = 16 * 60 * MINUTE
    events: list[Bar1s | Kline] = [
        _kline("1m", index * MINUTE, MINUTE) for index in range(16 * 60)
    ]
    events.extend(
        _kline("5m", minute_start - (15 - index) * 5 * MINUTE, 5 * MINUTE)
        for index in range(15)
    )

    bar_start = minute_start - MINUTE
    events.extend(_bar(bar_start + index * 1_000, "100", "100") for index in range(56))
    events.extend(
        _bar(
            bar_start + offset * 1_000,
            close,
            "120" if offset == 60 else close,
            "4",
        )
        for offset, close in enumerate(("100", "101", "102", "104", "106"), start=56)
    )
    # 信号确认后的下一根已完成 1s Bar 才创建三档预挂单。
    events.append(_bar(minute_start + 1_000, "110", "110"))
    return sorted(
        events,
        key=lambda event: (
            event.available_time,
            event.type_priority,
            event.symbol,
            event.sequence,
        ),
    )


class AccountStub:
    def __init__(self, position: Position | None = None):
        self.position = position

    def get_order(self, order_id):
        return None

    def iter_orders(self):
        return ()

    def has_open_position(self, symbol):
        return self.position is not None and self.position.symbol == symbol

    def get_position(self, symbol):
        return self.position if self.position is not None and self.position.symbol == symbol else None

    def cancel_order(self, order_id):
        return False

    async def flush_cancellations(self):
        return ()

    @property
    def has_pending_cancellations(self):
        return False

    def all_orders_terminal(self, symbol):
        return False


@dataclass
class Submission:
    intent: OrderIntent
    reduce_only: bool
    reference_price: Decimal


class ExecutorStub:
    def __init__(self):
        self.submissions: list[Submission] = []

    async def submit(self, intent, *, reference_price):
        self.submissions.append(Submission(intent, intent.reduce_only, reference_price))
        return type("OrderRecord", (), {"status": "NEW"})()


class CampaignStoreStub:
    async def get_active(self):
        return None

    async def acquire(self, lease):
        return True

    async def release(self, campaign_id):
        return False


def _coordinator(strategy, account, executor) -> SpikeExecutionCoordinator:
    gate = CompositeEntryGate(strategy)
    for condition in ("execution", "market", "subcategory", "campaign"):
        gate.set_condition(condition, True)
    return SpikeExecutionCoordinator(
        strategy=strategy,
        account=account,
        executor=executor,
        campaign_store=CampaignStoreStub(),
        risk_guard=RiskGuard("spike-test", RiskConfig()),
        gate=gate,
        account_id="spike-test",
    )


def _contract(intent: OrderIntent, *, reduce_only: bool) -> tuple:
    return (
        intent.symbol,
        intent.side,
        intent.order_type,
        intent.price,
        intent.quantity,
        intent.client_order_id,
        intent.strategy_id,
        reduce_only,
    )


@pytest.mark.asyncio
async def test_replay_and_live_emit_identical_three_tier_entry_contract():
    events = _entry_events()
    replay_strategy = DynamicSpikeBacktestStrategy(["BTCUSDT"], Decimal("1000"))
    live_strategy = DynamicSpikeBacktestStrategy(["BTCUSDT"], Decimal("1000"))
    live_account = AccountStub()
    executor = ExecutorStub()
    coordinator = _coordinator(live_strategy, live_account, executor)

    replay_intents: list[OrderIntent] = []
    try:
        for event in events:
            if isinstance(event, Kline):
                replay_intents.extend(replay_strategy.on_kline(event))
                await coordinator.on_kline(event)
            else:
                replay_intents.extend(replay_strategy.on_bar1s(event))
                await coordinator.on_bar1s(event)

        assert len(replay_intents) == len(executor.submissions) == 3
        assert [_contract(intent, reduce_only=False) for intent in replay_intents] == [
            _contract(submission.intent, reduce_only=submission.reduce_only)
            for submission in executor.submissions
        ]
        assert [intent.trigger_reason for intent in replay_intents] == [
            "spike_tier1",
            "spike_tier2",
            "spike_tier3",
        ]
        assert all(intent.side == "SELL" for intent in replay_intents)
        assert all(intent.order_type == "LIMIT" for intent in replay_intents)
        assert all(not intent.reduce_only for intent in replay_intents)
        assert all(not submission.reduce_only for submission in executor.submissions)
        assert all(
            submission.reference_price == submission.intent.price
            for submission in executor.submissions
        )
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_replay_and_live_do_not_prehang_exit_and_submit_it_reduce_only_market():
    position = Position(
        symbol="BTCUSDT",
        side="SHORT",
        entry_price=Decimal("100"),
        quantity=Decimal("1"),
        total_commission=Decimal("0.2"),
        unrealized_pnl=Decimal("0"),
        realized_pnl=Decimal("0"),
        opened_at=1_000,
    )
    replay_account = AccountStub(position)
    live_account = AccountStub(position)
    replay_strategy = DynamicSpikeBacktestStrategy(
        ["BTCUSDT"], Decimal("1000"), account=replay_account
    )
    live_strategy = DynamicSpikeBacktestStrategy(
        ["BTCUSDT"], Decimal("1000"), account=live_account
    )
    for strategy in (replay_strategy, live_strategy):
        symbol_strategy = strategy.strategies["BTCUSDT"]
        symbol_strategy.first_fill_time = 1_000
        symbol_strategy._campaign_id_for_timing = "spike_short:BTCUSDT:1"

    executor = ExecutorStub()
    coordinator = _coordinator(live_strategy, live_account, executor)
    campaign_id = "spike_short:BTCUSDT:1"
    coordinator._owned_campaign_id = campaign_id
    coordinator._owned_campaign_lease = CampaignLease(
        campaign_id, "spike_short", "BTCUSDT", 1
    )
    before_timeout = [_bar(timestamp, "110", "111") for timestamp in range(840_000, 900_000, 1_000)]
    timeout_bar = _bar(900_000, "110", "111")

    try:
        for bar in before_timeout:
            assert replay_strategy.on_bar1s(bar) == []
            await coordinator.on_bar1s(bar)
        assert executor.submissions == []

        replay_intents = replay_strategy.on_bar1s(timeout_bar)
        await coordinator.on_bar1s(timeout_bar)

        assert len(replay_intents) == len(executor.submissions) == 1
        submission = executor.submissions[0]
        assert _contract(replay_intents[0], reduce_only=True) == _contract(
            submission.intent, reduce_only=submission.reduce_only
        )
        assert replay_intents[0].trigger_reason == "campaign_timeout_exit"
        assert replay_intents[0].side == "BUY"
        assert replay_intents[0].order_type == "MARKET"
        assert replay_intents[0].reduce_only is True
        assert replay_intents[0].ttl_ms is None
        assert submission.reduce_only is True
        assert submission.reference_price == replay_intents[0].price
    finally:
        await coordinator.stop()
