from decimal import Decimal

import pytest

from trading_platform.backtest.strategy_definition import FeatureSpec
from trading_platform.shared.events import Bar1s, Kline
from trading_platform.strategies.spike.exit_features import CandidateFeatureConfig
from trading_platform.strategies.spike.shared_features import (
    SpikeSharedFeatureProvider,
)
from trading_platform.strategies.spike.short import DynamicSpikeBacktestStrategy
from trading_platform.strategies.spike.v1_1 import SpikeV11Strategy
from trading_platform.strategies.spike.v2 import SpikeV2Strategy
from trading_platform.strategies.spike.v2_1 import SpikeV21Strategy


def _bar(
    index: int,
    *,
    close: str = "100",
    volume: str = "1",
    buy_volume: str | None = None,
    sell_volume: str | None = None,
    raw_trade_count: int | None = None,
) -> Bar1s:
    price = Decimal(close)
    return Bar1s(
        symbol="BTCUSDT",
        timestamp=index * 1_000,
        available_time=(index + 1) * 1_000,
        open=price,
        high=price,
        low=price,
        close=price,
        volume=Decimal(volume),
        trade_count=1,
        vwap=price,
        raw_trade_count=raw_trade_count,
        taker_buy_volume=(
            None if buy_volume is None else Decimal(buy_volume)
        ),
        taker_sell_volume=(
            None if sell_volume is None else Decimal(sell_volume)
        ),
    )


def _kline(interval: str, index: int, duration: int) -> Kline:
    return Kline(
        symbol="BTCUSDT",
        interval=interval,
        open_time=index * duration,
        close_time=(index + 1) * duration - 1,
        available_time=(index + 1) * duration,
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100"),
        volume=Decimal("1"),
    )


def test_shared_bar_features_keep_exact_scalar_window_semantics():
    provider = SpikeSharedFeatureProvider(
        shared_features={FeatureSpec("rise_5s", "1s")}
    )
    bars = [_bar(index) for index in range(61)]
    bars[-1] = _bar(60, close="107", volume="15")
    for bar in bars:
        provider.process_event(bar)

    features = provider.bar_features(bars[-1])
    volume_features = provider.volume_features(bars[-1])

    assert features is not None
    assert features.continuous is True
    assert features.rise_5s == Decimal("0.07")
    assert volume_features is not None
    assert volume_features.volume_5s == Decimal("19")
    assert volume_features.median_volume_1s == Decimal("1")
    assert volume_features.volume_multiple_5s == Decimal("3.8")
    assert provider.volume_features(bars[-1]) is volume_features


def test_unrequested_1s_feature_has_no_1s_cache_or_calculation():
    provider = SpikeSharedFeatureProvider(
        shared_features={FeatureSpec("candidate_exit", "1m")}
    )
    bar = _bar(0)

    provider.process_event(bar)

    assert provider.bars_1s == []
    assert provider.bar_features(bar) is None


def test_orderflow_feature_builds_rolling_cvd_from_archivable_bar_fields():
    provider = SpikeSharedFeatureProvider(
        shared_features={FeatureSpec("orderflow", "1s")}
    )
    bars = [
        _bar(
            index,
            volume="3",
            buy_volume="2",
            sell_volume="1",
            raw_trade_count=3,
        )
        for index in range(301)
    ]
    for bar in bars:
        provider.process_event(bar)

    features = provider.orderflow_features(bars[-1])

    assert features is not None
    assert features.orderflow_ready is True
    assert features.taker_buy_volume_5s == Decimal("10")
    assert features.taker_sell_volume_5s == Decimal("5")
    assert features.raw_trade_count_5s == 15
    assert features.cvd_5s == Decimal("5")
    assert features.cvd_1m == Decimal("60")
    assert features.cvd_5m == Decimal("300")
    assert features.taker_buy_ratio_5s == Decimal("2") / Decimal("3")


def test_orderflow_feature_rejects_gapped_windows():
    provider = SpikeSharedFeatureProvider(
        shared_features={FeatureSpec("orderflow", "1s")}
    )
    bars = [
        _bar(
            index,
            volume="3",
            buy_volume="2",
            sell_volume="1",
            raw_trade_count=3,
        )
        for index in range(301)
        if index != 299
    ]
    for bar in bars:
        provider.process_event(bar)

    features = provider.orderflow_features(bars[-1])

    assert features is not None
    assert features.orderflow_ready is False
    assert features.cvd_5s is None
    assert features.cvd_1m is None
    assert features.cvd_5m is None


def test_spike_provider_rejects_features_owned_by_other_strategies():
    with pytest.raises(ValueError, match="unsupported Spike shared features"):
        SpikeSharedFeatureProvider(
            shared_features={FeatureSpec("other_strategy_signal", "1s")}
        )


def test_candidate_features_are_cached_per_kline_version(monkeypatch):
    calls = []

    def fake_snapshot(klines_1m, klines_5m, klines_15m, *, config):
        calls.append((tuple(klines_1m), tuple(klines_5m), tuple(klines_15m), config))
        return None

    monkeypatch.setattr(
        "trading_platform.strategies.spike.shared_features.candidate_feature_snapshot",
        fake_snapshot,
    )
    provider = SpikeSharedFeatureProvider(
        shared_features={FeatureSpec("candidate_exit", "1m")}
    )
    config = CandidateFeatureConfig()
    minute = _kline("1m", 0, 60_000)
    provider.process_event(minute)

    assert provider.candidate_features(config) is None
    assert provider.candidate_features(config) is None
    assert len(calls) == 1

    provider.process_event(_kline("5m", 0, 300_000))
    assert provider.candidate_features(config) is None
    assert len(calls) == 2


def test_shared_kline_cache_evicts_expired_late_candle():
    provider = SpikeSharedFeatureProvider(
        shared_features={FeatureSpec("candidate_exit", "1m")}
    )
    duration = 300_000
    retention = 40 * 60 * 60_000

    def kline_at(open_time: int) -> Kline:
        return Kline(
            symbol="BTCUSDT",
            interval="5m",
            open_time=open_time,
            close_time=open_time + duration - 1,
            available_time=open_time + duration,
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100"),
            volume=Decimal("1"),
        )

    expired = kline_at(0)
    boundary = kline_at(duration)
    latest = kline_at(retention + duration)
    late = kline_at(2 * duration)
    future = kline_at(retention + 3 * duration)

    for kline in (expired, boundary, latest, late, future):
        provider.process_event(kline)

    assert list(provider.klines_5m) == [latest, future]


def test_shared_features_match_isolated_strategies_with_different_thresholds():
    minute = 60_000
    klines = [
        Kline(
            symbol="BTCUSDT",
            interval="1m",
            open_time=index * minute,
            close_time=(index + 1) * minute - 1,
            available_time=(index + 1) * minute,
            open=Decimal("100"),
            high=Decimal("102"),
            low=Decimal("80"),
            close=Decimal("100"),
            volume=Decimal("10"),
        )
        for index in range(16 * 60)
    ]
    minute_start = 16 * 60 * minute
    klines.extend(
        Kline(
            symbol="BTCUSDT",
            interval="5m",
            open_time=minute_start - (15 - index) * 5 * minute,
            close_time=minute_start - (14 - index) * 5 * minute - 1,
            available_time=minute_start - (14 - index) * 5 * minute,
            open=Decimal("100"),
            high=Decimal("102"),
            low=Decimal("98"),
            close=Decimal("100"),
            volume=Decimal("10"),
        )
        for index in range(15)
    )
    bar_start = minute_start - minute
    closes = [Decimal("100")] * 56 + [
        Decimal("100"),
        Decimal("101"),
        Decimal("102"),
        Decimal("104"),
        Decimal("106"),
    ]
    bars = [
        Bar1s(
            symbol="BTCUSDT",
            timestamp=bar_start + index * 1_000,
            available_time=bar_start + (index + 1) * 1_000,
            open=close,
            high=Decimal("120") if index == 60 else close,
            low=close,
            close=close,
            volume=Decimal("4") if index >= 56 else Decimal("1"),
            trade_count=1,
            vwap=close,
        )
        for index, close in enumerate(closes)
    ]

    def adapter(threshold: str) -> DynamicSpikeBacktestStrategy:
        return DynamicSpikeBacktestStrategy(
            ["BTCUSDT"],
            total_notional=Decimal("1000"),
            strategy_class=SpikeV11Strategy,
            strategy_parameters={"rise_5s_threshold": Decimal(threshold)},
        )

    isolated = [adapter("0.05"), adapter("0.10")]
    for item in isolated:
        leaf = item.strategies["BTCUSDT"]
        for kline in klines:
            leaf.on_kline(kline)
        for bar in bars:
            leaf._update_cache(bar)

    shared = [adapter("0.05"), adapter("0.10")]
    provider = SpikeSharedFeatureProvider(
        shared_features={
            FeatureSpec("rise_5s", "1s"),
            FeatureSpec("candidate_exit", "1m"),
        }
    )
    for item in shared:
        provider.bind(item)
    for event in [*klines, *bars]:
        provider.process_event(event)

    isolated_signals = [
        item.strategies["BTCUSDT"]._detect_signal(bars[-1])
        for item in isolated
    ]
    shared_signals = [
        item.strategies["BTCUSDT"]._detect_signal(bars[-1])
        for item in shared
    ]

    assert shared_signals == isolated_signals
    assert shared_signals[0] is not None
    assert shared_signals[1] is None
    first = provider.bar_features(bars[-1])
    assert provider.bar_features(bars[-1]) is first


@pytest.mark.parametrize(
    ("box_duration", "rise_low", "expected_retention"),
    (
        (0, 3 * 60, 30 * 60),
        (60, 3 * 60, 7 * 24 * 60),
        (0, 72 * 60, 72 * 60),
    ),
)
def test_shared_provider_retention_covers_each_requirement_exactly(
    box_duration, rise_low, expected_retention
):
    adapter = DynamicSpikeBacktestStrategy(
        ["BTCUSDT"],
        total_notional=Decimal("1000"),
        strategy_class=SpikeV21Strategy,
        rise_low_lookback_minutes=rise_low,
        min_rise_duration_minutes=60,
        strategy_parameters={"box_duration_min_minutes": box_duration},
    )
    provider = SpikeSharedFeatureProvider(
        shared_features={
            FeatureSpec("rise_5s", "1s"),
            FeatureSpec("candidate_exit", "1m"),
        }
    )

    provider.bind(adapter)

    assert provider.retained_1m_minutes == expected_retention


def test_shared_provider_evicts_1m_candles_at_exact_retention_boundary():
    retained = 30 * 60
    provider = SpikeSharedFeatureProvider(
        shared_features={FeatureSpec("candidate_exit", "1m")},
        retained_1m_minutes=retained,
    )
    boundary = _kline("1m", 0, 60_000)
    expired = _kline("1m", -1, 60_000)
    latest = _kline("1m", retained, 60_000)

    provider.process_event(expired)
    provider.process_event(boundary)
    provider.process_event(latest)

    assert list(provider.klines_1m) == [boundary, latest]
