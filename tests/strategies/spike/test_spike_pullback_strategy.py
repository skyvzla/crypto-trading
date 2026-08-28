from decimal import Decimal

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
    assert strategy.drain_audit_events()[-1].event_type == "signal_expired"


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
