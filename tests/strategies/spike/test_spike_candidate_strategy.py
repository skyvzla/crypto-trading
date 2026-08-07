from decimal import Decimal
from unittest.mock import AsyncMock, Mock

import pytest

from trading_platform.shared.events import Bar1s, Kline, Position
from trading_platform.strategies.spike_exit_features import CandidateFeatureSnapshot
from trading_platform.strategies.spike_live import SpikeLiveSettings
from trading_platform.strategies.spike_main import SpikeLiveProcess
from trading_platform.strategies.spike_short import DynamicSpikeShortStrategy


FIRST_FILL_MS = 1_000


class PositionAccount:
    def __init__(self, quantity: str = "2"):
        self.orders = []
        self.position = Position(
            symbol="AKEUSDT",
            side="SHORT",
            entry_price=Decimal("100"),
            quantity=Decimal(quantity),
            total_commission=Decimal("0"),
            unrealized_pnl=Decimal("0"),
            realized_pnl=Decimal("0"),
            opened_at=FIRST_FILL_MS,
        )

    def get_position(self, symbol):
        return self.position if symbol == self.position.symbol else None

    def iter_orders(self):
        return tuple(self.orders)

    def cancel_order(self, order_id):
        for order in self.orders:
            if order.order_id == order_id and order.status in {
                "NEW",
                "PARTIALLY_FILLED",
            }:
                order.status = "CANCELLED"
                return True
        return False


def _features(
    *,
    agreement: int | None,
    breakout_5m: bool = False,
    breakout_15m: bool = False,
) -> CandidateFeatureSnapshot:
    return CandidateFeatureSnapshot(
        event_time=FIRST_FILL_MS,
        decay_agreement=agreement,
        stable_breakout_5m=breakout_5m,
        stable_breakout_15m=breakout_15m,
        down_channel_5m=True,
        down_channel_15m=True,
    )


def _strategy(*, agreement: int | None, quantity: str = "2"):
    account = PositionAccount(quantity)
    strategy = DynamicSpikeShortStrategy(
        "AKEUSDT",
        total_notional=Decimal("20"),
        account=account,
        exit_policy="candidate-v1",
    )
    strategy.restore_campaign_timing(
        "spike_short:AKEUSDT:1",
        FIRST_FILL_MS,
        origin_price=Decimal("90"),
    )
    strategy._candidate_features = _features(agreement=agreement)
    return strategy, account


def _evaluate(strategy, *, elapsed_ms: int, price: str):
    return strategy._manage_candidate_exit(
        FIRST_FILL_MS + elapsed_ms, Decimal(price)
    )


def test_origin_momentum_decay_reduces_half_only_once():
    strategy, _ = _strategy(agreement=2)

    first = _evaluate(strategy, elapsed_ms=20_000, price="90")
    second = _evaluate(strategy, elapsed_ms=30_000, price="89")

    assert len(first) == 1
    assert first[0].quantity == Decimal("1")
    assert first[0].reduce_only is True
    assert first[0].trigger_reason == "candidate_origin_reduce"
    assert second == []
    assert strategy.campaign_exit_state() == (True, True, False)
    assert [event.event_type for event in strategy.drain_audit_events()] == [
        "candidate_origin_check",
        "candidate_exit_requested",
    ]


def test_recovered_position_exit_does_not_wait_for_signal_bar_buffer():
    strategy, _ = _strategy(agreement=3)
    bar = Bar1s(
        symbol="AKEUSDT",
        timestamp=FIRST_FILL_MS + 90_000,
        available_time=FIRST_FILL_MS + 91_000,
        open=Decimal("95"),
        high=Decimal("95"),
        low=Decimal("95"),
        close=Decimal("95"),
        volume=Decimal("1"),
        trade_count=1,
        vwap=Decimal("95"),
    )

    intents = strategy.on_bar1s(bar)

    assert len(strategy.bars_1s) == 1
    assert len(intents) == 1
    assert intents[0].trigger_reason == "candidate_momentum_exit"


def test_candidate_exit_cancels_remaining_entries_before_requesting_order():
    strategy, account = _strategy(agreement=3)
    entry = Mock(
        order_id="entry-2",
        client_order_id="entry-client-2",
        status="NEW",
    )
    account.orders.append(entry)
    strategy.active_signals = [
        Mock(placed_client_order_ids={"entry-client-2"})
    ]

    first = _evaluate(strategy, elapsed_ms=90_000, price="95")
    second = _evaluate(strategy, elapsed_ms=91_000, price="95")

    assert first == []
    assert entry.status == "CANCELLED"
    assert len(second) == 1
    assert second[0].trigger_reason == "candidate_momentum_exit"


def test_origin_without_momentum_decay_holds_and_is_not_rechecked():
    strategy, _ = _strategy(agreement=1)

    assert _evaluate(strategy, elapsed_ms=20_000, price="90") == []
    strategy._candidate_features = _features(agreement=3)
    assert _evaluate(strategy, elapsed_ms=30_000, price="89") == []

    assert strategy.campaign_exit_state() == (True, False, False)
    audit = strategy.drain_audit_events()
    assert len(audit) == 1
    assert audit[0].event_type == "candidate_origin_check"
    assert audit[0].details["decision"] == "hold"


@pytest.mark.parametrize(
    ("elapsed_ms", "agreement", "should_exit"),
    [
        (89_999, 3, False),
        (90_000, 2, False),
        (90_000, 3, True),
        (299_999, 2, False),
        (300_000, 2, True),
        (899_999, 1, False),
        (900_000, 1, True),
    ],
)
def test_candidate_momentum_threshold_tightens_at_90_300_900_seconds(
    elapsed_ms, agreement, should_exit
):
    strategy, account = _strategy(agreement=agreement)

    intents = _evaluate(strategy, elapsed_ms=elapsed_ms, price="95")

    assert bool(intents) is should_exit
    if should_exit:
        assert intents[0].quantity == account.position.quantity
        assert intents[0].trigger_reason == "candidate_momentum_exit"
        assert strategy.campaign_exit_state() == (False, False, True)
    else:
        assert strategy.campaign_exit_state() == (False, False, False)


@pytest.mark.parametrize(
    ("elapsed_ms", "should_exit"),
    [(899_999, False), (900_000, True)],
)
def test_candidate_non_positive_time_risk_starts_at_900_seconds(
    elapsed_ms, should_exit
):
    strategy, account = _strategy(agreement=0)

    intents = _evaluate(strategy, elapsed_ms=elapsed_ms, price="105")

    assert bool(intents) is should_exit
    if should_exit:
        assert intents[0].quantity == account.position.quantity
        assert intents[0].trigger_reason == "candidate_time_risk_exit"


@pytest.mark.parametrize(
    ("field", "expected_reason"),
    [
        ("breakout_5m", "stable_trend_breakout_5m"),
        ("breakout_15m", "stable_trend_breakout_15m"),
    ],
)
def test_stable_5m_or_15m_breakout_exits_the_full_position(field, expected_reason):
    strategy, account = _strategy(agreement=None)
    feature_args = {"agreement": None, field: True}
    strategy._candidate_features = _features(**feature_args)

    intents = _evaluate(strategy, elapsed_ms=10_000, price="95")

    assert len(intents) == 1
    assert intents[0].quantity == account.position.quantity
    assert intents[0].reduce_only is True
    assert intents[0].trigger_reason == "candidate_trend_exit"
    audit = strategy.drain_audit_events()
    assert audit[-1].details["reason"] == expected_reason
    assert strategy.campaign_exit_state() == (False, False, True)


def _kline(interval: str, open_time: int, *, price: str = "100") -> Kline:
    duration = {"1m": 60_000, "5m": 300_000, "15m": 900_000}[interval]
    value = Decimal(price)
    return Kline(
        symbol="AKEUSDT",
        interval=interval,
        open_time=open_time,
        close_time=open_time + duration - 1,
        available_time=open_time + duration,
        open=value,
        high=value + 1,
        low=value - 1,
        close=value,
        volume=Decimal("1"),
    )


def test_candidate_strategy_caches_15m_and_only_recomputes_during_campaign(monkeypatch):
    captured = []

    def capture(one_minute, five_minute, fifteen_minute, *, config):
        captured.append((tuple(one_minute), tuple(five_minute), tuple(fifteen_minute)))
        return None

    monkeypatch.setattr(
        "trading_platform.strategies.spike_short.candidate_feature_snapshot",
        capture,
    )
    account = PositionAccount()
    strategy = DynamicSpikeShortStrategy(
        "AKEUSDT", Decimal("20"), account=account, exit_policy="candidate-v1"
    )
    cached_while_flat = _kline("15m", 0)

    assert strategy.on_kline(cached_while_flat) == []
    assert strategy.klines_15m == [cached_while_flat]
    assert captured == []

    strategy.restore_campaign_timing(
        "spike_short:AKEUSDT:1",
        FIRST_FILL_MS,
        origin_price=Decimal("90"),
    )
    candle_during_campaign = _kline("15m", 900_000)

    assert strategy.on_kline(candle_during_campaign) == []

    assert strategy.klines_15m == [cached_while_flat, candle_during_campaign]
    assert captured == [((), (), (cached_while_flat, candle_during_campaign))]


@pytest.mark.asyncio
async def test_live_market_subscription_requests_15m_klines():
    settings = SpikeLiveSettings(
        account_id="spike-test",
        symbols=["AKEUSDT"],
        total_notional=Decimal("20"),
        exit_policy="candidate-v1",
    )
    process = SpikeLiveProcess(
        settings,
        binance=Mock(),
        database=Mock(),
        redis_config=Mock(),
        strategy_config=Mock(account_id="spike-test"),
    )
    response = Mock()
    process.http = Mock(put=AsyncMock(return_value=response))

    await process._register_market_subscriptions()

    process.http.put.assert_awaited_once_with(
        f"/subscriptions/{process._consumer_id}",
        json={
            "symbols": ["AKEUSDT"],
            "types": ["bar1s", "kline:1m", "kline:5m", "kline:15m"],
        },
    )
    response.raise_for_status.assert_called_once_with()
