from decimal import Decimal

import pytest

from trading_platform.backtest.engine import BacktestEngine
from trading_platform.shared.config import BacktestConfig
from trading_platform.shared.events import Bar1s, Fill, Kline, OrderIntent, Position
from trading_platform.strategies.spike.pullback import (
    PullbackV3Strategy,
    PullbackV3BacktestStrategy,
    _PendingEntry,
)
from trading_platform.strategies.spike.definition import (
    SPIKE_CANDIDATE_EXIT_FEATURE,
    SPIKE_MIN_LOW_1M_FEATURE,
    SPIKE_PRIOR_HIGH_1M_FEATURE,
    SPIKE_RISE_60S_FEATURE,
    SPIKE_V2_SHARED_METRICS,
)
from trading_platform.strategies.spike.shared_features import (
    SpikeSharedFeatureProvider,
)
from trading_platform.strategies.spike.exit_features import (
    CandidateFeatureSnapshot,
)


SYMBOL = "BTCUSDT"


class MutablePositionAccount:
    def __init__(self):
        self.position = None

    def get_position(self, symbol):
        return self.position if symbol == SYMBOL else None

    def has_open_position(self, symbol):
        position = self.get_position(symbol)
        return position is not None and position.quantity > 0


def _fill(side: str, quantity: str, fill_time: int) -> Fill:
    return Fill(
        fill_id=f"fill-{side.lower()}-{fill_time}",
        order_id=f"order-{side.lower()}-{fill_time}",
        symbol=SYMBOL,
        side=side,
        price=Decimal("100"),
        quantity=Decimal(quantity),
        commission=Decimal("0"),
        commission_asset="USDT",
        fill_time=fill_time,
        is_maker=False,
    )


def _position(quantity: str) -> Position:
    return Position(
        symbol=SYMBOL,
        side="SHORT",
        entry_price=Decimal("100"),
        quantity=Decimal(quantity),
        total_commission=Decimal("0"),
        unrealized_pnl=Decimal("0"),
        realized_pnl=Decimal("0"),
        opened_at=1_000,
    )


def _bar(available_time: int) -> Bar1s:
    return Bar1s(
        symbol=SYMBOL,
        timestamp=available_time - 1_000,
        available_time=available_time,
        open=Decimal("100"),
        high=Decimal("102"),
        low=Decimal("99"),
        close=Decimal("100"),
        volume=Decimal("1"),
        trade_count=1,
        vwap=Decimal("100"),
    )


def _bar_at(
    timestamp: int,
    *,
    close: str = "100",
    high: str | None = None,
    low: str | None = None,
    open_price: str | None = None,
    volume: str = "0",
) -> Bar1s:
    price = Decimal(close)
    return Bar1s(
        symbol=SYMBOL,
        timestamp=timestamp,
        available_time=timestamp + 1_000,
        open=Decimal(open_price or close),
        high=Decimal(high or close),
        low=Decimal(low or close),
        close=price,
        volume=Decimal(volume),
        trade_count=0,
        vwap=price,
    )


def _start_campaign(quantity: str = "2"):
    account = MutablePositionAccount()
    strategy = PullbackV3Strategy(SYMBOL, Decimal("200"), account=account)
    strategy._pending_entry_meta = _PendingEntry(
        signal_ms=900,
        origin_price=Decimal("80"),
        spike_high=Decimal("120"),
    )
    account.position = _position(quantity)
    strategy.on_fill(_fill("SELL", quantity, 1_000))
    return strategy, account


def _start_backtest_campaign(*, limit_fill_fraction_per_bar: float = 1.0):
    strategy = PullbackV3Strategy(SYMBOL, Decimal("200"))
    engine = BacktestEngine(
        strategy,
        [],
        BacktestConfig(
            taker_fee_rate=0,
            maker_fee_rate=0,
            limit_fill_fraction_per_bar=limit_fill_fraction_per_bar,
        ),
    )
    strategy._pending_entry_meta = _PendingEntry(
        signal_ms=900,
        origin_price=Decimal("80"),
        spike_high=Decimal("120"),
    )
    engine.executor.place_order(
        OrderIntent(
            symbol=SYMBOL,
            side="SELL",
            price=Decimal("100"),
            quantity=Decimal("2"),
            client_order_id="pullback-entry",
            order_type="MARKET",
            campaign_id="pullback_v3:BTCUSDT:900",
        )
    )
    engine.process_event(_bar(1_000))
    return strategy, engine


def _kline(interval: str, open_time: int) -> Kline:
    duration = {"1m": 60_000, "5m": 300_000, "15m": 900_000}[interval]
    return Kline(
        symbol=SYMBOL,
        interval=interval,
        open_time=open_time,
        close_time=open_time + duration - 1,
        available_time=open_time + duration,
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100"),
        volume=Decimal("1"),
    )


def test_full_buy_fill_resets_campaign_and_stops_candidate_refresh(monkeypatch):
    strategy, engine = _start_backtest_campaign()
    strategy._candidate_features = object()
    refresh_calls = []
    monkeypatch.setattr(
        "trading_platform.strategies.spike.pullback.candidate_feature_snapshot",
        lambda *args, **kwargs: refresh_calls.append((args, kwargs)),
    )

    engine.executor.place_order(
        OrderIntent(
            symbol=SYMBOL,
            side="BUY",
            price=Decimal("100"),
            quantity=Decimal("2"),
            client_order_id="pullback-exit",
            order_type="MARKET",
            reduce_only=True,
            campaign_id="pullback_v3:BTCUSDT:900",
        )
    )
    engine.process_event(_bar(2_000))

    assert engine.get_position(SYMBOL) is None
    assert strategy.first_fill_time is None
    assert strategy.entry_price is None
    assert strategy._campaign_origin_price is None
    assert strategy._candidate_features is None
    assert strategy._active_campaign_id is None
    assert engine.audit_records[-1].campaign_id == (
        "pullback_v3:BTCUSDT:900"
    )

    for interval in ("1m", "5m", "15m"):
        strategy.on_kline(_kline(interval, 3_000))

    assert refresh_calls == []


def test_second_entry_fill_initializes_a_new_campaign_after_full_exit():
    strategy, account = _start_campaign()
    account.position = None
    strategy.on_fill(_fill("BUY", "2", 2_000))
    strategy._pending_entry_meta = _PendingEntry(
        signal_ms=2_900,
        origin_price=Decimal("90"),
        spike_high=Decimal("135"),
    )

    account.position = _position("3")
    strategy.on_fill(_fill("SELL", "3", 3_000))

    assert strategy.first_fill_time == 3_000
    assert strategy.entry_price == Decimal("100")
    assert strategy._campaign_origin_price == Decimal("90")
    assert strategy._spike_high == Decimal("135")
    assert strategy._campaign_id_for_timing == "pullback_v3:BTCUSDT:2900"


def test_partial_buy_fill_preserves_campaign_and_exit_state(monkeypatch):
    strategy, engine = _start_backtest_campaign(limit_fill_fraction_per_bar=0.5)
    strategy._candidate_features = object()
    strategy._candidate_peak_price = Decimal("90")
    strategy._candidate_exit_state.exit_requested = True
    monkeypatch.setattr(strategy, "_candidate_exit", lambda *args: [])

    engine.executor.place_order(
        OrderIntent(
            symbol=SYMBOL,
            side="BUY",
            price=Decimal("101"),
            quantity=Decimal("2"),
            client_order_id="pullback-partial-exit",
            order_type="LIMIT",
            reduce_only=True,
            campaign_id="pullback_v3:BTCUSDT:900",
        )
    )
    engine.process_event(_bar(2_000))

    assert engine.get_position(SYMBOL).quantity == Decimal("1")
    assert strategy.first_fill_time == 1_000
    assert strategy.entry_price == Decimal("100")
    assert strategy._campaign_origin_price == Decimal("80")
    assert strategy._candidate_features is not None
    assert strategy._candidate_peak_price == Decimal("90")
    assert strategy._candidate_exit_state.exit_requested is True
    audit = engine.audit_records[-1]
    assert audit.event_type == "pullback_exit_filled"
    assert audit.campaign_id == "pullback_v3:BTCUSDT:900"
    assert Decimal(audit.details["quantity"]) == Decimal("1")


def test_zero_quantity_position_clears_campaign():
    strategy, account = _start_campaign()
    account.position = _position("0")

    strategy.on_fill(_fill("BUY", "2", 2_000))

    assert strategy.first_fill_time is None
    assert strategy._active_campaign_id is None


def test_entry_exit_intents_and_audits_share_signal_campaign_id():
    account = MutablePositionAccount()
    strategy = PullbackV3Strategy(SYMBOL, Decimal("200"), account=account)
    strategy._pending = _PendingEntry(
        signal_ms=900,
        origin_price=Decimal("80"),
        spike_high=Decimal("120"),
    )

    entry = strategy._advance_pending(_bar(1_000))[0]
    account.position = _position("2")
    strategy.on_fill(_fill("SELL", "2", 1_100))
    exit_intent = strategy._exit_intent(
        Decimal("95"), "take_profit", 1_500, Decimal("2")
    )
    account.position = None
    strategy.on_fill(_fill("BUY", "2", 2_000))

    campaign_id = "pullback_v3:BTCUSDT:900"
    assert entry.campaign_id == campaign_id
    assert exit_intent.campaign_id == campaign_id
    assert {
        event.campaign_id
        for event in strategy.drain_audit_events()
        if event.event_type
        in {
            "pullback_entry_placed",
            "pullback_entry_filled",
            "pullback_exit_requested",
            "pullback_exit_filled",
        }
    } == {campaign_id}


def test_moving_60s_rise_triggers_without_3s_volume_conditions():
    strategy = PullbackV3Strategy(
        SYMBOL,
        Decimal("200"),
        rise_low_lookback_hours=0,
        min_rise_duration_hours=0,
        prior_high_lookback_hours=0,
        min_spike_rise=Decimal("0"),
    )

    for index in range(61):
        close = "130" if index == 60 else "100"
        strategy.on_bar1s(
            _bar_at(index * 1_000, close=close, high=close, low=close)
        )

    assert strategy._pending is None

    strategy.on_bar1s(_bar_at(61_000, close="140", high="140", low="140"))

    assert strategy._pending is not None
    assert strategy._pending.signal_ms == 61_000
    assert strategy._pending.origin_price == Decimal("100")
    audit = strategy.drain_audit_events()[-1]
    assert audit.event_type == "signal_triggered"
    assert Decimal(audit.details["rise_60s"]) == Decimal("0.4")


def test_local_moving_60s_rise_rejects_a_gap_without_scanning_the_window():
    strategy = PullbackV3Strategy(
        SYMBOL,
        Decimal("200"),
        rise_low_lookback_hours=0,
        min_rise_duration_hours=0,
        prior_high_lookback_hours=0,
        min_spike_rise=Decimal("0"),
    )

    for bar in [_bar_at(index * 1_000) for index in range(60)]:
        strategy.on_bar1s(bar)
    final_bar = _bar_at(61_000, close="140", high="140", low="140")
    strategy.on_bar1s(final_bar)

    assert strategy._continuous_1s_count == 1
    assert strategy._pending is None


def test_custom_rise_window_with_shared_provider_still_rejects_a_gap():
    strategy = PullbackV3Strategy(
        SYMBOL,
        Decimal("200"),
        rise_window_seconds=300,
        rise_60s_threshold=Decimal("0.30"),
        rise_low_lookback_hours=0,
        min_rise_duration_hours=0,
        prior_high_lookback_hours=0,
        min_spike_rise=Decimal("0"),
    )
    provider = SpikeSharedFeatureProvider(
        shared_features={SPIKE_RISE_60S_FEATURE},
        shared_metrics=frozenset(),
        retained_1m_minutes=1,
    )
    strategy.bind_shared_feature_provider(provider)

    for index in range(300):
        strategy.on_bar1s(_bar_at(index * 1_000))
    strategy.on_bar1s(
        _bar_at(301_000, close="140", high="140", low="140")
    )

    assert strategy._continuous_1s_count == 1
    assert strategy._pending is None


def test_pending_signal_expires_after_90_seconds():
    strategy = PullbackV3Strategy(
        SYMBOL,
        Decimal("200"),
        rise_low_lookback_hours=0,
        min_rise_duration_hours=0,
        prior_high_lookback_hours=0,
        wait_seconds=90,
    )
    strategy._pending = _PendingEntry(
        signal_ms=0,
        origin_price=Decimal("100"),
        spike_high=Decimal("120"),
    )

    assert strategy._advance_pending(
        _bar_at(90_001, close="120", high="120", low="120")
    ) == []
    assert strategy._pending is None
    audit = strategy.drain_audit_events()[-1]
    assert audit.event_type == "signal_expired"
    assert audit.details["timeout_stage"] == "retrace_not_reached"
    assert audit.details["retrace_reached"] is False


def test_pending_timeout_records_prior_high_funnel_stage():
    strategy = PullbackV3Strategy(
        SYMBOL,
        Decimal("200"),
        prior_high_lookback_hours=4,
        wait_seconds=1,
    )
    strategy._pending = _PendingEntry(
        signal_ms=0,
        origin_price=Decimal("100"),
        spike_high=Decimal("150"),
        prior_high=Decimal("140"),
    )

    assert strategy._advance_pending(
        _bar_at(1_000, close="135", high="150", low="135")
    ) == []
    assert strategy._advance_pending(
        _bar_at(2_000, close="140", high="150", low="140")
    ) == []

    audit = strategy.drain_audit_events()[-1]
    assert audit.event_type == "signal_expired"
    assert audit.details["timeout_stage"] == "prior_high_not_cleared"
    assert audit.details["retrace_reached"] is True


def test_pending_origin_breach_is_audited():
    strategy = PullbackV3Strategy(SYMBOL, Decimal("200"))
    strategy._pending = _PendingEntry(
        signal_ms=0,
        origin_price=Decimal("100"),
        spike_high=Decimal("150"),
    )

    assert strategy._advance_pending(
        _bar_at(1_000, close="99", high="101", low="99")
    ) == []

    audit = strategy.drain_audit_events()[-1]
    assert audit.event_type == "signal_invalidated"
    assert audit.details["reason"] == "origin_breached"


def test_pending_entry_requires_candidate_strictly_above_prior_high():
    def pending(prior_high: str) -> PullbackV3Strategy:
        strategy = PullbackV3Strategy(
            SYMBOL,
            Decimal("200"),
            rise_low_lookback_hours=0,
            min_rise_duration_hours=0,
            prior_high_lookback_hours=4,
            min_spike_rise=Decimal("0.4"),
            retrace_frac=Decimal("0.3"),
        )
        strategy._pending = _PendingEntry(
            signal_ms=0,
            origin_price=Decimal("100"),
            spike_high=Decimal("150"),
            prior_high=Decimal(prior_high),
        )
        return strategy

    bar = _bar_at(
        1_000,
        close="140",
        open_price="130",
        high="150",
        low="135",
    )
    equal = pending("135")
    assert equal._advance_pending(bar) == []
    assert equal._pending is not None

    above = pending("134")
    intents = above._advance_pending(bar)
    assert len(intents) == 1
    assert intents[0].price == Decimal("135")


def test_fixed_take_profit_is_disabled_by_default_for_candidate_v1():
    strategy, account = _start_campaign()
    assert strategy.take_profit == Decimal("0")

    exit_intents = strategy._manage_exits(
        _bar_at(2_000, close="80", high="81", low="80")
    )

    assert exit_intents == []
    assert account.position is not None


def _stable_breakout_snapshot(*, five_m=False, fifteen_m=True) -> CandidateFeatureSnapshot:
    return CandidateFeatureSnapshot(
        event_time=1_001,
        decay_agreement=1,
        stable_breakout_5m=five_m,
        stable_breakout_15m=fifteen_m,
        down_channel_5m=False,
        down_channel_15m=False,
    )


def test_stable_breakout_age_gate_holds_position_inside_age_window():
    """入场后 age 内即使站稳破位已点亮也不退出（修复假破位早退）。"""
    strategy, account = _start_campaign()  # first_fill_time=1000
    strategy.exit_stable_breakout_age_ms = 60_000
    strategy._candidate_features = _stable_breakout_snapshot(fifteen_m=True)
    strategy._campaign_origin_price = Decimal("80")

    # elapsed = 2_000-1_000 = 1s < 60s → 稳定破位被 gate 掉
    exit_intents = strategy._manage_exits(
        _bar_at(2_000, close="80", high="81", low="80")
    )

    assert exit_intents == []
    assert account.position is not None


def test_stable_breakout_age_gate_releases_after_age_window():
    """超过最短持有期后，稳定破位正常触发退出。"""
    strategy, account = _start_campaign()  # first_fill_time=1000
    strategy.exit_stable_breakout_age_ms = 60_000
    strategy._candidate_features = _stable_breakout_snapshot(fifteen_m=True)
    strategy._campaign_origin_price = Decimal("80")

    # elapsed = 62_000-1_000 = 61s >= 60s → 稳定破位放行
    exit_intents = strategy._manage_exits(
        _bar_at(62_000, close="80", high="81", low="80")
    )

    assert len(exit_intents) == 1
    assert exit_intents[0].trigger_reason == "candidate_trend_exit"
    assert account.position is not None  # 订单已提交，待成交


def test_stable_breakout_age_zero_keeps_original_behavior():
    """age=0 (默认) 保持原行为：入场即触发稳定破位。"""
    strategy, account = _start_campaign()  # first_fill_time=1000
    strategy._candidate_features = _stable_breakout_snapshot(fifteen_m=True)
    strategy._campaign_origin_price = Decimal("80")

    exit_intents = strategy._manage_exits(
        _bar_at(1_001, close="80", high="81", low="80")
    )

    assert len(exit_intents) == 1
    assert exit_intents[0].trigger_reason == "candidate_trend_exit"


def test_long_rise_window_catches_slower_rise_that_60s_misses():
    """rise_window_seconds=300 时，300s 累计涨 30%（慢牛）能触发信号，60s 窗口看不到。"""
    strategy = PullbackV3Strategy(
        SYMBOL,
        Decimal("200"),
        rise_window_seconds=300,
        rise_60s_threshold=Decimal("0.30"),
        rise_low_lookback_hours=0,
        min_rise_duration_hours=0,
        prior_high_lookback_hours=0,
        min_spike_rise=Decimal("0"),
    )
    # 前 240 根: 100 → 130（240s 累计涨 30%）
    # 第 300 根: 130 → 133 时, 300s 窗口(bar[0]=100) 累计涨 33% ≥ 30%
    for index in range(300):
        close = "130" if index >= 240 else "100"
        strategy.on_bar1s(
            _bar_at(index * 1_000, close=close, high=close, low=close)
        )
    # 60s 窗口此刻 bar[0]≈127(240s处) → 涨幅 (133/127-1)≈4.7% <30%; 300s窗口 bar[0]=100 → 33% >=30%
    strategy.on_bar1s(_bar_at(300_000, close="133", high="133", low="133"))

    assert strategy._pending is not None
    assert strategy._pending.origin_price == Decimal("100")
    audit = strategy.drain_audit_events()[-1]
    assert audit.event_type == "signal_triggered"
    assert Decimal(audit.details["rise_60s"]) > Decimal("0.30")


def test_default_window_60_keeps_original_behavior():
    """默认 rise_window_seconds=60 保持原行为：缓牛(300s累计33%)但60s只涨~6% → 不触发。"""
    strategy = PullbackV3Strategy(
        SYMBOL,
        Decimal("200"),
        rise_60s_threshold=Decimal("0.30"),
        rise_low_lookback_hours=0,
        min_rise_duration_hours=0,
        prior_high_lookback_hours=0,
        min_spike_rise=Decimal("0"),
    )
    # 线性爬升: close = 100 + index*0.11, index0..300 → 100..133 (300s累计33%)
    # 相邻60s窗口: 100+120*0.11=113.2 → 100+180*0.11=119.8, 涨幅5.8%<30% → 60s不触发任何时刻
    for index in range(301):
        close = f"{100 + index * 0.11:.2f}"
        strategy.on_bar1s(
            _bar_at(index * 1_000, close=close, high=close, low=close)
        )

    assert strategy._pending is None


@pytest.mark.parametrize("rise_window_seconds", [59, 3601])
def test_rise_window_rejects_values_outside_supported_range(
    rise_window_seconds,
):
    with pytest.raises(
        ValueError, match="rise_window_seconds must be between 60 and 3600"
    ):
        PullbackV3Strategy(
            SYMBOL,
            Decimal("200"),
            rise_window_seconds=rise_window_seconds,
        )


def test_breakout_gate_stop_triggers_on_deep_adverse_within_age():
    """age 门期间逆势浮亏达阈值即止损（防止 MON 被 gate 时猛烈反抽继续亏）。"""
    strategy, account = _start_campaign()  # first_fill_time=1000, entry_price=100
    strategy.exit_stable_breakout_age_ms = 60_000
    strategy.stable_breakout_gate_stop_pct = Decimal("0.20")
    strategy._candidate_features = _stable_breakout_snapshot(fifteen_m=True)
    strategy._campaign_origin_price = Decimal("80")

    # elapsed=1s < 60s(在 age 内), close=125 ⇒ 相对 entry100 逆势+25% ≥ 20% ⇒ gate_stop
    exit_intents = strategy._manage_exits(
        _bar_at(2_000, close="125", high="126", low="125")
    )
    assert len(exit_intents) == 1
    assert exit_intents[0].trigger_reason == "candidate_gate_stop"
    assert strategy._candidate_exit_state.exit_requested is True
    assert strategy._manage_exits(
        _bar_at(3_000, close="126", high="127", low="126")
    ) == []


def test_breakout_gate_stop_requires_a_stable_breakout_to_be_gated():
    strategy, _ = _start_campaign()
    strategy.exit_stable_breakout_age_ms = 60_000
    strategy.stable_breakout_gate_stop_pct = Decimal("0.20")
    strategy._candidate_features = _stable_breakout_snapshot(
        five_m=False, fifteen_m=False
    )
    strategy._campaign_origin_price = Decimal("80")

    assert strategy._manage_exits(
        _bar_at(2_000, close="125", high="126", low="125")
    ) == []
    assert strategy._candidate_exit_state.exit_requested is False


def test_breakout_gate_stop_not_trigger_within_budget():
    """age 门期间逆势 8%(<20%) 不触发 gate_stop，继续被 gate 持有。"""
    strategy, account = _start_campaign()
    strategy.exit_stable_breakout_age_ms = 60_000
    strategy.stable_breakout_gate_stop_pct = Decimal("0.20")
    strategy._candidate_features = _stable_breakout_snapshot(fifteen_m=True)
    strategy._campaign_origin_price = Decimal("80")

    # elapsed=1s < 60s, close=108 ⇒ +8% < 20% ⇒ gate 期内不触发（也不被 stable 踢出）
    exit_intents = strategy._manage_exits(
        _bar_at(2_000, close="108", high="109", low="108")
    )
    assert exit_intents == []


def test_breakout_gate_stop_disabled_by_default():
    """gate_stop 默认 None(关闭)：age 门期间深逆势不额外止损（原方案C行为）。"""
    strategy, account = _start_campaign()
    strategy.exit_stable_breakout_age_ms = 60_000
    strategy._candidate_features = _stable_breakout_snapshot(fifteen_m=True)
    strategy._campaign_origin_price = Decimal("80")

    exit_intents = strategy._manage_exits(
        _bar_at(2_000, close="125", high="126", low="125")
    )
    # 无 gate_stop：仍在 age 门内，stable 被 gate，不退出
    assert exit_intents == []


def test_pullback_backtest_adapter_binds_shared_provider_to_leaf_strategies():
    adapter = PullbackV3BacktestStrategy([SYMBOL], Decimal("200"))
    provider = SpikeSharedFeatureProvider(
        shared_features={
            SPIKE_RISE_60S_FEATURE,
            SPIKE_CANDIDATE_EXIT_FEATURE,
            SPIKE_MIN_LOW_1M_FEATURE,
            SPIKE_PRIOR_HIGH_1M_FEATURE,
        },
        shared_metrics=SPIKE_V2_SHARED_METRICS,
        retained_1m_minutes=30 * 60,
    )

    provider.bind(adapter)

    assert adapter.strategies[SYMBOL]._shared_feature_provider is provider
