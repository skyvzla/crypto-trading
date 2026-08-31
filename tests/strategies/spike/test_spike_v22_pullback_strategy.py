from decimal import Decimal

import pytest

from trading_platform.backtest import run_spike_short
from trading_platform.shared.events import Bar1s
from trading_platform.strategies.spike.short import SpikeSignal
from trading_platform.strategies.spike.v2_2_pullback import (
    SpikeV22PullbackStrategy,
    V22Pullback,
)


def _strategy(**kwargs) -> SpikeV22PullbackStrategy:
    return SpikeV22PullbackStrategy(
        "BTCUSDT",
        total_notional=Decimal("1000"),
        entry_tier_mode="tier3-only",
        max_consecutive_up_minutes=0,
        min_spike_rise=Decimal("0.15"),
        retrace_frac=Decimal("0.30"),
        wait_seconds=90,
        **kwargs,
    )


def _signal() -> SpikeSignal:
    return SpikeSignal(
        signal_time=1_000,
        trigger_price=Decimal("118"),
        spike_high=Decimal("120"),
        origin_price=Decimal("80"),
        atr=Decimal("10"),
        tier_prices=[Decimal("108"), Decimal("112"), Decimal("116")],
        tier_weights=[Decimal("0"), Decimal("0"), Decimal("1")],
        invalid_price=Decimal("150"),
        active_time=2_000,
        expire_time=91_000,
        origin_floor=Decimal("88"),
        prior_high=Decimal("105"),
        rise_from_12h_low=Decimal("1.2"),
        impulse_base_price=Decimal("100"),
        pullback_spike_high=Decimal("120"),
        pullback_last_time=1_000,
    )


def _bar(timestamp: int, *, open_: str, high: str, low: str) -> Bar1s:
    return Bar1s(
        symbol="BTCUSDT",
        timestamp=timestamp,
        available_time=timestamp + 1_000,
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(open_),
        volume=Decimal("1"),
        trade_count=1,
        vwap=Decimal(open_),
    )


def test_retrace_touch_arms_then_enters_at_next_continuous_open():
    strategy = _strategy()
    signal = _signal()
    strategy.active_signals.append(signal)

    touch_intents = strategy._manage_signals(
        _bar(2_000, open_="118", high="119", low="113")
    )
    entry_intents = strategy._manage_signals(
        _bar(3_000, open_="112", high="113", low="111")
    )

    assert touch_intents == []
    assert signal.pullback_candidate == Decimal("114")
    assert signal.pullback_ready_time == 2_000
    assert signal.pullback_time == 2_000
    assert signal.pullback_low == Decimal("113")
    assert len(entry_intents) == 1
    assert entry_intents[0].order_type == "MARKET"
    assert entry_intents[0].price == Decimal("112")
    assert entry_intents[0].quantity * entry_intents[0].price == Decimal("1000")
    assert entry_intents[0].trigger_reason == "pullback_entry"


def test_current_bar_high_only_changes_next_bar_candidate():
    strategy = _strategy()
    signal = _signal()
    signal.pullback_spike_high = Decimal("110")
    strategy.active_signals.append(signal)

    assert strategy._manage_signals(
        _bar(2_000, open_="110", high="130", low="109")
    ) == []
    assert signal.pullback_ready_time is None
    assert signal.pullback_spike_high == Decimal("130")

    assert strategy._manage_signals(
        _bar(3_000, open_="125", high="126", low="120")
    ) == []
    assert signal.pullback_candidate == Decimal("121")
    assert signal.pullback_ready_time == 3_000


@pytest.mark.parametrize(
    ("bar", "reason"),
    [
        (_bar(2_000, open_="99", high="101", low="99"), "base"),
        (_bar(92_000, open_="118", high="119", low="117"), "timeout"),
    ],
)
def test_pending_signal_is_removed_on_base_breach_or_timeout(bar, reason):
    strategy = _strategy()
    signal = _signal()
    strategy.active_signals.append(signal)

    assert strategy._manage_signals(bar) == []
    assert signal not in strategy.active_signals
    terminal = strategy.drain_audit_events()[-1]
    assert reason in terminal.event_type or reason in str(terminal.details)


def test_candidate_below_prior_high_does_not_arm():
    strategy = _strategy(prior_high_tolerance_percent=Decimal("0"))
    signal = _signal()
    signal.prior_high = Decimal("115")
    strategy.active_signals.append(signal)

    assert strategy._manage_signals(
        _bar(2_000, open_="118", high="119", low="113")
    ) == []
    assert signal.pullback_ready_time is None


def test_ready_signal_requires_next_continuous_bar():
    strategy = _strategy()
    signal = _signal()
    signal.pullback_candidate = Decimal("114")
    signal.pullback_ready_time = 2_000
    signal.pullback_last_time = 2_000
    strategy.active_signals.append(signal)

    assert strategy._manage_signals(
        _bar(4_000, open_="112", high="113", low="111")
    ) == []
    assert signal not in strategy.active_signals


def test_v22_invalid_price_still_terminates_pending_pullback():
    strategy = _strategy()
    signal = _signal()
    strategy.active_signals.append(signal)

    assert strategy._manage_signals(
        _bar(2_000, open_="149", high="150", low="140")
    ) == []
    assert signal not in strategy.active_signals


def test_retrace_touch_at_deadline_cannot_enter_after_deadline():
    strategy = _strategy()
    signal = _signal()
    signal.expire_time = 2_000
    strategy.active_signals.append(signal)

    assert strategy._manage_signals(
        _bar(2_000, open_="118", high="119", low="113")
    ) == []
    assert signal not in strategy.active_signals


def test_campaign_origin_and_impulse_base_have_independent_floors():
    strategy = _strategy()
    signal = _signal()
    signal.origin_price = Decimal("110")
    signal.origin_floor = Decimal("121")
    signal.impulse_base_price = Decimal("100")

    assert strategy._entry_price_allowed(signal, Decimal("120")) is False
    assert strategy._entry_price_allowed(signal, Decimal("122")) is True


def test_definition_preserves_v22_data_and_adds_pullback_parameters():
    assert V22Pullback.name == "v2.2-pullback"
    assert V22Pullback.data_requirements.metrics_5m is True
    assert {"min_spike_rise", "retrace_frac", "wait_seconds"} <= (
        V22Pullback.supported_parameters
    )


def test_backtest_engine_passes_pullback_and_bucket_parameters(monkeypatch):
    args = run_spike_short.parse_args([
        "--symbol", "BTCUSDT",
        "--start", "2026-01-01T00:00:00+00:00",
        "--end", "2026-01-02T00:00:00+00:00",
        "--total-notional", "1000",
        "--duckdb-path", "candles.duckdb",
        "--metrics-root", "metrics",
        "--strategy",
        "trading_platform.strategies.spike.v2_2_pullback:V22Pullback",
        "--entry-tier-mode", "tier3-only",
        "--min-spike-rise", "0.15",
        "--retrace-frac", "0.30",
        "--wait-seconds", "90",
        "--group-rise-12h-threshold", "1.0",
        "--strong-bucket-strict-age-ms", "1800000",
        "--weak-bucket-strict-age-ms", "900000",
    ])
    settings = run_spike_short.resolve_settings(args)
    monkeypatch.setattr(run_spike_short, "load_symbol_rules", lambda *_: None)

    engine = run_spike_short.create_spike_engine(
        args,
        settings,
        [],
        preloaded_metrics_series=[(0, 1.0, 1.0)],
    )
    strategy = engine.strategy.strategies["BTCUSDT"]

    assert strategy.min_spike_rise == Decimal("0.15")
    assert strategy.retrace_frac == Decimal("0.30")
    assert strategy.wait_ms == 90_000
    assert strategy._candidate_exit_config.strong_bucket_strict_age_ms == 1_800_000
    assert strategy._candidate_exit_config.weak_bucket_strict_age_ms == 900_000
