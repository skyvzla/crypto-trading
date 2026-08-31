from decimal import Decimal
from unittest.mock import AsyncMock, Mock

import pytest

from trading_platform.shared.events import Bar1s, Kline, Position
from trading_platform.strategies.spike.exit_features import CandidateFeatureSnapshot
from trading_platform.strategies.spike.live import SpikeLiveSettings
from trading_platform.strategies.spike.main import SpikeLiveProcess
from trading_platform.strategies.spike.short import DynamicSpikeShortStrategy
from trading_platform.strategies.universe import ExchangeSymbolSnapshot


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


def _strategy(
    *,
    agreement: int | None,
    quantity: str = "2",
    early_profit_unlock_ratio: Decimal | None = None,
    profit_drawdown_peak_ratio: Decimal | None = None,
    profit_drawdown_ratio: Decimal | None = None,
    strong_bucket_strict_age_ms: int | None = None,
    weak_bucket_strict_age_ms: int | None = None,
    hard_stop_loss_pct: Decimal | None = None,
    hard_stop_confirm_ms: int = 0,
):
    account = PositionAccount(quantity)
    strategy = DynamicSpikeShortStrategy(
        "AKEUSDT",
        total_notional=Decimal("20"),
        account=account,
        exit_policy="candidate-v1",
        early_profit_unlock_ratio=early_profit_unlock_ratio,
        profit_drawdown_peak_ratio=profit_drawdown_peak_ratio,
        profit_drawdown_ratio=profit_drawdown_ratio,
        strong_bucket_strict_age_ms=strong_bucket_strict_age_ms,
        weak_bucket_strict_age_ms=weak_bucket_strict_age_ms,
        hard_stop_loss_pct=hard_stop_loss_pct,
        hard_stop_confirm_ms=hard_stop_confirm_ms,
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


def test_hard_stop_cancels_entries_then_requests_one_persisted_exit():
    strategy, account = _strategy(
        agreement=None,
        hard_stop_loss_pct=Decimal("0.08"),
    )
    entry = Mock(
        order_id="entry-2",
        client_order_id="entry-client-2",
        status="NEW",
    )
    account.orders.append(entry)
    strategy.active_signals = [Mock(placed_client_order_ids={"entry-client-2"})]

    assert _evaluate(strategy, elapsed_ms=10_000, price="108") == []
    assert entry.status == "CANCELLED"
    intents = _evaluate(strategy, elapsed_ms=11_000, price="108")
    duplicate = _evaluate(strategy, elapsed_ms=12_000, price="109")

    assert len(intents) == 1
    assert intents[0].trigger_reason == "candidate_hard_stop_exit"
    assert intents[0].quantity == account.position.quantity
    assert duplicate == []
    assert strategy.campaign_exit_state() == (False, False, True)


def test_hard_stop_confirmation_requires_continuous_breach_until_recovery():
    strategy, _ = _strategy(
        agreement=None,
        hard_stop_loss_pct=Decimal("0.08"),
        hard_stop_confirm_ms=5_000,
    )

    assert _evaluate(strategy, elapsed_ms=10_000, price="108") == []
    assert strategy._hard_stop_armed is True
    assert _evaluate(strategy, elapsed_ms=14_999, price="109") == []
    assert _evaluate(strategy, elapsed_ms=15_000, price="100") == []
    assert strategy._hard_stop_armed is False

    assert _evaluate(strategy, elapsed_ms=20_000, price="108") == []
    intents = _evaluate(strategy, elapsed_ms=25_000, price="108")

    assert len(intents) == 1
    assert intents[0].trigger_reason == "candidate_hard_stop_exit"


@pytest.mark.parametrize(
    ("loss_pct", "confirm_ms", "message"),
    [
        (Decimal("0"), 0, "hard_stop_loss_pct"),
        (Decimal("1.01"), 0, "hard_stop_loss_pct"),
        (Decimal("0.08"), -1, "hard_stop_confirm_ms"),
    ],
)
def test_hard_stop_parameters_are_validated(loss_pct, confirm_ms, message):
    with pytest.raises(ValueError, match=message):
        DynamicSpikeShortStrategy(
            "AKEUSDT",
            total_notional=Decimal("20"),
            exit_policy="candidate-v1",
            hard_stop_loss_pct=loss_pct,
            hard_stop_confirm_ms=confirm_ms,
        )


def test_recovered_candidate_cancels_wal_entry_without_active_signal():
    strategy, account = _strategy(agreement=3)
    entry = Mock(
        order_id="entry-1",
        client_order_id="s_AKEUSDT_1_e1",
        status="NEW",
        reduce_only=False,
    )
    account.orders.append(entry)

    assert _evaluate(strategy, elapsed_ms=90_000, price="95") == []
    assert entry.status == "CANCELLED"
    assert _evaluate(strategy, elapsed_ms=91_000, price="95")[0].reduce_only is True


def test_recovered_candidate_waits_for_unknown_entry_without_mutating_exit_state():
    strategy, account = _strategy(agreement=3)
    account.orders.append(
        Mock(
            order_id="entry-unknown",
            client_order_id="s_AKEUSDT_1_e1",
            status="SUBMIT_UNKNOWN",
            reduce_only=False,
        )
    )

    assert _evaluate(strategy, elapsed_ms=90_000, price="95") == []
    assert strategy.campaign_exit_state() == (False, False, False)


def test_candidate_exit_blocks_campaign_rotation_until_exit_is_settled():
    strategy, account = _strategy(agreement=3)
    strategy._candidate_exit_waiting = True
    rotation = Mock()
    bar = Mock(available_time=FIRST_FILL_MS + 900_000, close=Decimal("95"))

    assert strategy._prepare_rotation(rotation, bar) == []

    strategy._candidate_exit_waiting = False
    strategy._candidate_exit_state.exit_requested = True
    assert strategy._prepare_rotation(rotation, bar) == []


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


def _push_1m_close(strategy, *, open_time: int, close: str) -> None:
    strategy.klines_1m.append(
        Kline(
            symbol="AKEUSDT",
            interval="1m",
            open_time=open_time,
            close_time=open_time + 59_999,
            available_time=open_time + 60_000,
            open=Decimal(close),
            high=Decimal(close),
            low=Decimal(close),
            close=Decimal(close),
            volume=Decimal("1"),
        )
    )


def test_profit_drawdown_peak_ratio_arms_only_after_peak_gain():
    strategy, account = _strategy(
        agreement=None,
        profit_drawdown_peak_ratio=Decimal("0.20"),
        profit_drawdown_ratio=Decimal("0.10"),
    )

    _push_1m_close(strategy, open_time=FIRST_FILL_MS, close="85")
    assert _evaluate(strategy, elapsed_ms=100_000, price="85") == []
    _push_1m_close(strategy, open_time=FIRST_FILL_MS + 60_000, close="80")
    assert _evaluate(strategy, elapsed_ms=110_000, price="80") == []
    _push_1m_close(strategy, open_time=FIRST_FILL_MS + 120_000, close="88")
    intents = _evaluate(strategy, elapsed_ms=120_000, price="88")

    assert len(intents) == 1
    assert intents[0].quantity == account.position.quantity
    assert intents[0].trigger_reason == "candidate_profit_drawdown_exit"
    assert strategy.campaign_exit_state() == (False, False, True)


def test_profit_drawdown_peak_ratio_requires_arming_then_drawdown():
    strategy, _ = _strategy(
        agreement=None,
        profit_drawdown_peak_ratio=Decimal("0.20"),
        profit_drawdown_ratio=Decimal("0.10"),
    )

    _push_1m_close(strategy, open_time=FIRST_FILL_MS, close="95")
    assert _evaluate(strategy, elapsed_ms=100_000, price="95") == []
    _push_1m_close(strategy, open_time=FIRST_FILL_MS + 60_000, close="80")
    assert _evaluate(strategy, elapsed_ms=110_000, price="80") == []
    _push_1m_close(strategy, open_time=FIRST_FILL_MS + 120_000, close="84")
    assert _evaluate(strategy, elapsed_ms=120_000, price="84") == []
    _push_1m_close(strategy, open_time=FIRST_FILL_MS + 180_000, close="88.1")
    intents = _evaluate(strategy, elapsed_ms=180_000, price="88.1")

    assert len(intents) == 1
    assert intents[0].trigger_reason == "candidate_profit_drawdown_exit"


def test_profit_drawdown_peak_ratio_does_not_weaken_momentum_tiers():
    strategy, _ = _strategy(
        agreement=1,
        profit_drawdown_peak_ratio=Decimal("0.20"),
        profit_drawdown_ratio=Decimal("0.10"),
    )

    _push_1m_close(strategy, open_time=FIRST_FILL_MS, close="80")
    assert _evaluate(strategy, elapsed_ms=100_000, price="80") == []
    assert strategy._candidate_drawdown_armed is True
    _push_1m_close(strategy, open_time=FIRST_FILL_MS + 60_000, close="82")
    assert _evaluate(strategy, elapsed_ms=120_000, price="82") == []


def test_static_bucket_strong_extends_time_risk_hold():
    strategy, _ = _strategy(
        agreement=None,
        strong_bucket_strict_age_ms=1_500_000,
        weak_bucket_strict_age_ms=600_000,
    )
    strategy._candidate_entry_bucket = "strong"

    assert _evaluate(strategy, elapsed_ms=1_200_000, price="101") == []
    intents = _evaluate(strategy, elapsed_ms=1_500_000, price="101")

    assert len(intents) == 1
    assert intents[0].trigger_reason == "candidate_time_risk_exit"
    assert strategy.campaign_exit_state() == (False, False, True)


def test_static_bucket_weak_cuts_time_risk_early():
    strategy, _ = _strategy(
        agreement=None,
        strong_bucket_strict_age_ms=1_500_000,
        weak_bucket_strict_age_ms=600_000,
    )
    strategy._candidate_entry_bucket = "weak"

    assert _evaluate(strategy, elapsed_ms=599_000, price="101") == []
    intents = _evaluate(strategy, elapsed_ms=600_000, price="101")

    assert len(intents) == 1
    assert intents[0].trigger_reason == "candidate_time_risk_exit"


def test_profit_threshold_permanently_unlocks_risk_exit_before_ninety_seconds():
    strategy, _ = _strategy(
        agreement=0,
        early_profit_unlock_ratio=Decimal("0.015"),
    )

    assert _evaluate(strategy, elapsed_ms=29_000, price="98.5") == []
    assert strategy.drain_audit_events() == []
    assert _evaluate(strategy, elapsed_ms=30_000, price="98.4") == []
    strategy._candidate_features = _features(agreement=3)
    intents = _evaluate(strategy, elapsed_ms=31_000, price="101")

    assert len(intents) == 1
    assert intents[0].trigger_reason == "candidate_momentum_exit"
    audit = strategy.drain_audit_events()
    assert [event.event_type for event in audit] == [
        "candidate_early_risk_unlocked",
        "candidate_exit_requested",
    ]


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
        "trading_platform.strategies.spike.short.candidate_feature_snapshot",
        capture,
    )
    account = PositionAccount()
    strategy = DynamicSpikeShortStrategy(
        "AKEUSDT", Decimal("20"), account=account, exit_policy="candidate-v1"
    )
    cached_while_flat = _kline("15m", 0)

    assert strategy.on_kline(cached_while_flat) == []
    assert list(strategy.klines_15m) == [cached_while_flat]
    assert captured == []

    strategy.restore_campaign_timing(
        "spike_short:AKEUSDT:1",
        FIRST_FILL_MS,
        origin_price=Decimal("90"),
    )
    candle_during_campaign = _kline("15m", 900_000)

    assert strategy.on_kline(candle_during_campaign) == []

    assert list(strategy.klines_15m) == [cached_while_flat, candle_during_campaign]
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
    process.exchange_symbol_snapshot = ExchangeSymbolSnapshot(
        allowed_symbols=frozenset({"AKEUSDT"}),
        blocked_symbols=frozenset(),
        blocked_reasons={},
    )

    await process._register_market_subscriptions()

    process.http.put.assert_awaited_once_with(
        f"/subscriptions/{process._consumer_id}",
        json={
            "symbols": ["AKEUSDT"],
            "types": ["bar1s", "kline:1m", "kline:5m", "kline:15m"],
        },
    )
    response.raise_for_status.assert_called_once_with()
