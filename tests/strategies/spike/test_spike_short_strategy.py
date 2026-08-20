"""
测试Dynamic Spike Short Strategy
"""
from decimal import Decimal
from collections import deque

import pytest
from trading_platform.shared.events import Bar1s, Kline, Order, Position
from trading_platform.strategies.spike.short import (
    DynamicSpikeBacktestStrategy,
    DynamicSpikeShortStrategy,
    SpikeSignal,
)
from trading_platform.strategies.spike.v1_1 import SpikeV11Strategy
from trading_platform.strategies.spike.v2_1 import SpikeV21Strategy


class TestDynamicSpikeShortStrategy:
    """测试逼空做空策略"""

    def test_strategy_creation(self):
        """测试策略创建"""
        strategy = DynamicSpikeShortStrategy(
            "BTCUSDT", total_notional=Decimal("1000")
        )
        assert strategy.symbol == "BTCUSDT"
        assert len(strategy.bars_1s) == 0
        assert len(strategy.klines_1m) == 0
        assert len(strategy.klines_5m) == 0

    def test_cache_update(self):
        """测试缓存更新"""
        strategy = DynamicSpikeShortStrategy(
            "BTCUSDT", total_notional=Decimal("1000")
        )

        # 添加60个1s Bar
        for i in range(70):
            bar = Bar1s(
                symbol="BTCUSDT",
                timestamp=1609459200000 + i * 1000,
                available_time=1609459201000 + i * 1000,
                open=Decimal("30000"),
                high=Decimal("30100"),
                low=Decimal("29900"),
                close=Decimal("30050"),
                volume=Decimal("1.0"),
                trade_count=10,
                vwap=Decimal("30025"),
            )
            strategy._update_cache(bar)

        # 信号计算需要当前 Bar 以及 60 秒前的 Bar
        assert len(strategy.bars_1s) == 61

    def test_kline_update(self):
        """测试K线更新"""
        strategy = DynamicSpikeShortStrategy(
            "BTCUSDT", total_notional=Decimal("1000")
        )

        # 添加1分钟K线
        kline_1m = Kline(
            symbol="BTCUSDT",
            interval="1m",
            open_time=1609459200000,
            close_time=1609459259999,
            available_time=1609459260000,
            open=Decimal("30000"),
            high=Decimal("30100"),
            low=Decimal("29900"),
            close=Decimal("30050"),
            volume=Decimal("10.0"),
        )
        strategy.on_kline(kline_1m)
        assert len(strategy.klines_1m) == 1

        # 添加5分钟K线
        kline_5m = Kline(
            symbol="BTCUSDT",
            interval="5m",
            open_time=1609459200000,
            close_time=1609459499999,
            available_time=1609459500000,
            open=Decimal("30000"),
            high=Decimal("30200"),
            low=Decimal("29800"),
            close=Decimal("30100"),
            volume=Decimal("50.0"),
        )
        strategy.on_kline(kline_5m)
        assert len(strategy.klines_5m) == 1

    @pytest.mark.parametrize(
        ("interval", "retained_minutes", "strategy_kwargs"),
        [
            ("1m", 30 * 60, {}),
            (
                "1m",
                7 * 24 * 60,
                {
                    "rise_low_lookback_minutes": 7 * 24 * 60,
                    "min_rise_duration_minutes": 24 * 60,
                },
            ),
            ("5m", 40 * 60, {}),
            ("15m", 40 * 60, {}),
        ],
    )
    def test_kline_cache_evicts_expired_head_in_place(
        self, interval, retained_minutes, strategy_kwargs
    ):
        """缓存保留 cutoff 边界 K 线，并以队列头部淘汰过期 K 线。"""
        duration_ms = {"1m": 60_000, "5m": 300_000, "15m": 900_000}[interval]
        strategy = DynamicSpikeShortStrategy(
            "BTCUSDT", total_notional=Decimal("1000"), **strategy_kwargs
        )
        cache_name = f"klines_{interval}"
        cache = getattr(strategy, cache_name)

        def kline_at(open_time: int) -> Kline:
            return Kline(
                symbol="BTCUSDT",
                interval=interval,
                open_time=open_time,
                close_time=open_time + duration_ms - 1,
                available_time=open_time + duration_ms,
                open=Decimal("100"),
                high=Decimal("101"),
                low=Decimal("99"),
                close=Decimal("100"),
                volume=Decimal("1"),
            )

        expired = kline_at(0)
        boundary = kline_at(duration_ms)
        latest = kline_at(retained_minutes * 60_000 + duration_ms)
        for kline in (expired, boundary, latest):
            strategy.on_kline(kline)

        assert isinstance(cache, deque)
        assert getattr(strategy, cache_name) is cache
        assert list(cache) == [boundary, latest]

    def test_kline_cache_evicts_expired_late_candle(self):
        """迟到但仍在窗口内的 K 线随后过期时，也必须从缓存移除。"""
        interval = "5m"
        duration_ms = 300_000
        retention_ms = 40 * 60 * 60_000
        strategy = DynamicSpikeShortStrategy(
            "BTCUSDT", total_notional=Decimal("1000")
        )

        def kline_at(open_time: int) -> Kline:
            return Kline(
                symbol="BTCUSDT",
                interval=interval,
                open_time=open_time,
                close_time=open_time + duration_ms - 1,
                available_time=open_time + duration_ms,
                open=Decimal("100"),
                high=Decimal("101"),
                low=Decimal("99"),
                close=Decimal("100"),
                volume=Decimal("1"),
            )

        expired = kline_at(0)
        boundary = kline_at(duration_ms)
        latest = kline_at(retention_ms + duration_ms)
        late = kline_at(2 * duration_ms)
        future = kline_at(retention_ms + 3 * duration_ms)
        for kline in (expired, boundary, latest, late, future):
            assert strategy.on_kline(kline) == []

        assert list(strategy.klines_5m) == [latest, future]

    def test_signal_detection_insufficient_data(self):
        """测试数据不足时不产生信号"""
        strategy = DynamicSpikeShortStrategy(
            "BTCUSDT", total_notional=Decimal("1000")
        )

        # 只有10个Bar，不足60个
        for i in range(10):
            bar = Bar1s(
                symbol="BTCUSDT",
                timestamp=1609459200000 + i * 1000,
                available_time=1609459201000 + i * 1000,
                open=Decimal("30000"),
                high=Decimal("30100"),
                low=Decimal("29900"),
                close=Decimal("30050"),
                volume=Decimal("1.0"),
                trade_count=10,
                vwap=Decimal("30025"),
            )
            strategy._update_cache(bar)

        current_bar = Bar1s(
            symbol="BTCUSDT",
            timestamp=1609459210000,
            available_time=1609459211000,
            open=Decimal("30000"),
            high=Decimal("31500"),
            low=Decimal("30000"),
            close=Decimal("31500"),
            volume=Decimal("100.0"),
            trade_count=1000,
            vwap=Decimal("31000"),
        )

        signal = strategy._detect_signal(current_bar)
        assert signal is None  # 数据不足

    def test_strategy_parameters(self):
        """测试策略参数"""
        assert DynamicSpikeShortStrategy.SPIKE_RISE_5S == Decimal("0.05")
        assert DynamicSpikeShortStrategy.VOLUME_MULTIPLE_5S == Decimal("3.0")
        assert DynamicSpikeShortStrategy.RISE_FROM_12H_LOW == Decimal("0.20")
        assert DynamicSpikeShortStrategy.PRIOR_HIGH_LOOKBACK_MINUTES == 240
        custom = DynamicSpikeShortStrategy(
            "BTCUSDT",
            total_notional=Decimal("1000"),
            prior_high_lookback_minutes=8 * 60,
        )
        assert custom.prior_high_lookback_minutes == 8 * 60
        assert len(DynamicSpikeShortStrategy.TIER_WEIGHTS) == 3
        legacy_cap = DynamicSpikeShortStrategy(
            "BTCUSDT",
            total_notional=Decimal("1000"),
            max_rise_5s_percent=Decimal("6"),
        )
        generic_cap = DynamicSpikeShortStrategy(
            "BTCUSDT",
            total_notional=Decimal("1000"),
            max_rise_window_seconds=5,
            max_rise_window_percent=Decimal("6"),
        )
        assert legacy_cap.max_rise_window == generic_cap.max_rise_window
        assert legacy_cap.max_rise_window_seconds == generic_cap.max_rise_window_seconds

    def test_rise_cap_window_returns_only_use_current_and_historical_bars(self):
        strategy = DynamicSpikeShortStrategy(
            "BTCUSDT", total_notional=Decimal("1000"),
            max_rise_window_seconds=15,
            max_rise_window_percent=Decimal("10"),
        )
        bars = [
            Bar1s(
                symbol="BTCUSDT",
                timestamp=index * 1_000,
                available_time=(index + 1) * 1_000,
                open=Decimal("100"),
                high=Decimal("100"),
                low=Decimal("100"),
                close=Decimal("100") if index < 46 else Decimal("120"),
                volume=Decimal("1"),
                trade_count=1,
                vwap=Decimal("100"),
            )
            for index in range(61)
        ]

        rises = strategy._rise_window_returns(bars, bars[-1])

        assert rises == {
            5: Decimal("0"),
            10: Decimal("0"),
            15: Decimal("0.2"),
            60: Decimal("0.2"),
        }

    @pytest.mark.parametrize("seconds", [0, 61, 5.5])
    def test_rise_cap_window_requires_supported_integer_seconds(self, seconds):
        with pytest.raises(ValueError, match="max_rise_window_seconds"):
            DynamicSpikeShortStrategy(
                "BTCUSDT",
                total_notional=Decimal("1000"),
                max_rise_window_seconds=seconds,
            )

    def test_consecutive_up_minutes_counter(self):
        """测试连续上涨根数倒推计数：close > open 计 1，否则中断。"""
        minute = 60_000
        strategy = SpikeV21Strategy(
            "BTCUSDT", total_notional=Decimal("1000")
        )
        strategy.klines_1m = [
            Kline(
                symbol="BTCUSDT",
                interval="1m",
                open_time=index * minute,
                close_time=(index + 1) * minute - 1,
                available_time=(index + 1) * minute,
                open=Decimal("100"),
                high=Decimal("120"),
                low=Decimal("90"),
                close=Decimal("103" if index < 2 else "99"),
                volume=Decimal("1"),
            )
            for index in range(4)
        ]
        assert strategy._consecutive_up_minutes() == 0

        strategy.klines_1m = [
            Kline(
                symbol="BTCUSDT",
                interval="1m",
                open_time=index * minute,
                close_time=(index + 1) * minute - 1,
                available_time=(index + 1) * minute,
                open=Decimal("100"),
                high=Decimal("120"),
                low=Decimal("90"),
                close=Decimal("101"),
                volume=Decimal("1"),
            )
            for index in range(4)
        ]
        assert strategy._consecutive_up_minutes() == 4

    @pytest.mark.parametrize("strategy_class", [SpikeV11Strategy, SpikeV21Strategy])
    def test_consecutive_up_filter_exposes_auditable_rejection(self, strategy_class):
        strategy = strategy_class(
            "BTCUSDT",
            total_notional=Decimal("1000"),
            max_consecutive_up_minutes=3,
        )
        strategy.klines_1m = [
            Kline(
                symbol="BTCUSDT",
                interval="1m",
                open_time=index * 60_000,
                close_time=(index + 1) * 60_000 - 1,
                available_time=(index + 1) * 60_000,
                open=Decimal("1"),
                high=Decimal("2"),
                low=Decimal("1"),
                close=Decimal("2"),
                volume=Decimal("1"),
            )
            for index in range(4)
        ]

        passed, details = strategy._entry_filter_decision(4 * 60_000)

        assert passed is False
        assert details == {
            "rejection_stage": "consecutive_up_entry_filter",
            "rejection_reasons": ["max_consecutive_up_minutes"],
            "consecutive_up_minutes": 4,
            "max_consecutive_up_minutes": 3,
        }

    @pytest.mark.parametrize("strategy_class", [SpikeV11Strategy, SpikeV21Strategy])
    def test_combined_entry_filters_preserve_all_rejection_details(
        self, strategy_class
    ):
        event_time = 4 * 60_000
        strategy = strategy_class(
            "BTCUSDT",
            total_notional=Decimal("1000"),
            max_consecutive_up_minutes=3,
            max_ls_ratio=1.5,
            metrics_series=[
                (0, 100.0, 1.0),
                (event_time, 130.0, 1.8),
            ],
        )
        strategy.klines_1m = [
            Kline(
                symbol="BTCUSDT",
                interval="1m",
                open_time=index * 60_000,
                close_time=(index + 1) * 60_000 - 1,
                available_time=(index + 1) * 60_000,
                open=Decimal("1"),
                high=Decimal("2"),
                low=Decimal("1"),
                close=Decimal("2"),
                volume=Decimal("1"),
            )
            for index in range(4)
        ]

        passed, details = strategy._entry_filter_decision(event_time)

        assert passed is False
        assert details == {
            "rejection_stage": "combined_entry_filters",
            "rejection_reasons": [
                "max_consecutive_up_minutes",
                "max_ls_ratio",
            ],
            "consecutive_up_minutes": 4,
            "max_consecutive_up_minutes": 3,
            "oi": 130.0,
            "previous_oi": 100.0,
            "oi_change_pct": 30.0,
            "ls_ratio": 1.8,
            "metrics_available_time": event_time,
            "max_oi_change_pct": 0.0,
            "max_ls_ratio": 1.5,
        }

    def test_top_maturity_filter_uses_native_five_minute_context(self):
        strategy = SpikeV21Strategy(
            "BTCUSDT",
            total_notional=Decimal("1000"),
            min_td_sell_setup_5m=4,
            min_volume_multiple_5m=Decimal("5"),
        )
        strategy.klines_5m = [
            Kline(
                symbol="BTCUSDT",
                interval="5m",
                open_time=index * 300_000,
                close_time=(index + 1) * 300_000 - 1,
                available_time=(index + 1) * 300_000,
                open=Decimal("99") + index,
                high=Decimal("102") + index,
                low=Decimal("98") + index,
                close=Decimal("100") + index,
                volume=Decimal("40") if index == 12 else Decimal("10"),
            )
            for index in range(13)
        ]

        passed, details = strategy._entry_filter_decision(13 * 300_000)

        assert passed is False
        assert details == {
            "rejection_stage": "top_maturity_entry_filter",
            "rejection_reasons": ["min_volume_multiple_5m"],
            "td_sell_setup_5m": 9,
            "min_td_sell_setup_5m": 4,
            "volume_multiple_5m": "4",
            "min_volume_multiple_5m": "5",
        }

        strategy.min_volume_multiple_5m = Decimal("4")
        assert strategy._entry_filter_decision(13 * 300_000) == (True, None)

    def test_consecutive_up_filter_rejects_long_streaks(self):
        """max_consecutive_up_minutes 设置后，连续上涨超过上限的信号被拒绝。"""
        minute = 60_000
        lookback_minutes = 7 * 24 * 60
        minute_start = lookback_minutes * minute
        low_open_time = minute_start - lookback_minutes * minute

        def build_strategy(max_consecutive: int) -> SpikeV21Strategy:
            strategy = SpikeV21Strategy(
                "BTCUSDT",
                total_notional=Decimal("1000"),
                prior_high_lookback_minutes=6 * 60,
                rise_low_lookback_minutes=lookback_minutes,
                min_rise_duration_minutes=24 * 60,
                max_consecutive_up_minutes=max_consecutive,
            )
            strategy.klines_1m = [
                Kline(
                    symbol="BTCUSDT",
                    interval="1m",
                    open_time=index * minute,
                    close_time=(index + 1) * minute - 1,
                    available_time=(index + 1) * minute,
                    open=Decimal("100"),
                    high=Decimal("102"),
                    low=Decimal("80")
                    if index * minute == low_open_time
                    else Decimal("85"),
                    close=Decimal("101"),  # 全部收阳，连续上涨
                    volume=Decimal("1"),
                )
                for index in range(lookback_minutes)
            ]
            strategy.klines_5m = [
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
                    volume=Decimal("1"),
                )
                for index in range(15)
            ]
            closes = [Decimal("100")] * 56 + [
                Decimal("100"), Decimal("101"), Decimal("102"),
                Decimal("104"), Decimal("106"),
            ]
            strategy.bars_1s = [
                Bar1s(
                    symbol="BTCUSDT",
                    timestamp=minute_start - (60 - index) * 1_000,
                    available_time=minute_start - (59 - index) * 1_000,
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
            return strategy

        filtered = build_strategy(max_consecutive=3)
        assert filtered._consecutive_up_minutes() > 3
        assert filtered._detect_signal(filtered.bars_1s[-1]) is None

        unfiltered = build_strategy(max_consecutive=0)
        assert unfiltered._consecutive_up_minutes() > 3
        assert unfiltered._detect_signal(unfiltered.bars_1s[-1]) is not None

    def test_consecutive_up_parameter_validation(self):
        with pytest.raises(ValueError):
            SpikeV21Strategy(
                "BTCUSDT",
                total_notional=Decimal("1000"),
                max_consecutive_up_minutes=-1,
            )

    def test_metrics_snapshot_alignment(self):
        """指标快照按事件时间对齐：取不晚于事件时刻的最近 5m 桶。"""
        series = [
            (1_000_000, 100.0, 1.2),
            (1_300_000, 110.0, 1.1),
            (1_600_000, 120.0, 0.9),
        ]
        strategy = SpikeV21Strategy(
            "BTCUSDT",
            total_notional=Decimal("1000"),
            metrics_series=series,
        )
        assert strategy._metrics_snapshot_at(1_250_000) == (100.0, 100.0, 1.2)
        assert strategy._metrics_snapshot_at(1_350_000) == (110.0, 100.0, 1.1)
        assert strategy._metrics_snapshot_at(1_750_000) == (120.0, 110.0, 0.9)

        fresh = SpikeV21Strategy(
            "BTCUSDT", total_notional=Decimal("1000"), metrics_series=series
        )
        assert fresh._metrics_snapshot_at(1_000_000 - 1) is None  # 早于首个快照

    def test_metrics_blocked_by_oi_change(self):
        """OI 5m 变化超过上限时拦截信号。"""
        series = [
            (1_000_000, 100.0, 1.0),
            (1_300_000, 130.0, 1.0),  # +30%
        ]
        strategy = SpikeV21Strategy(
            "BTCUSDT",
            total_notional=Decimal("1000"),
            metrics_series=series,
            max_oi_change_pct=15.0,
        )
        assert strategy._metrics_blocked(1_350_000) is True   # +30% > 15%
        strategy.max_oi_change_pct = 50.0
        assert strategy._metrics_blocked(1_350_000) is False  # +30% < 50%
        strategy.max_oi_change_pct = 0.0
        assert strategy._metrics_blocked(1_350_000) is False  # 禁用

    def test_metrics_blocked_by_ls_ratio(self):
        """多空比超过上限时拦截信号。"""
        series = [
            (1_000_000, 100.0, 1.8),
            (1_300_000, 100.0, 1.8),
        ]
        strategy = SpikeV21Strategy(
            "BTCUSDT",
            total_notional=Decimal("1000"),
            metrics_series=series,
            max_ls_ratio=1.5,
        )
        assert strategy._metrics_blocked(1_350_000) is True   # 1.8 > 1.5
        strategy.max_ls_ratio = 2.0
        assert strategy._metrics_blocked(1_350_000) is False  # 1.8 < 2.0
        assert sum(DynamicSpikeShortStrategy.TIER_WEIGHTS) == Decimal("1.0")

    def test_prior_high_uses_configured_lookback_window(self):
        minute = 60_000
        minute_start = 10_000_000_000
        strategy = DynamicSpikeShortStrategy(
            "BTCUSDT",
            total_notional=Decimal("1000"),
            prior_high_lookback_minutes=8 * 60,
        )
        strategy.klines_1m = [
            Kline(
                symbol="BTCUSDT",
                interval="1m",
                open_time=minute_start - (480 - index) * minute,
                close_time=minute_start - (480 - index) * minute + minute - 1,
                available_time=minute_start - (480 - index - 1) * minute,
                open=Decimal("100"),
                high=Decimal("200") if index == 100 else Decimal("150"),
                low=Decimal("90"),
                close=Decimal("100"),
                volume=Decimal("1"),
            )
            for index in range(480)
        ]

        prior_high, _ = strategy._prior_high_point(minute_start)

        assert prior_high == Decimal("200")

    def test_v11_entry_threshold_and_prior_high_tolerance_are_configurable(self):
        from trading_platform.strategies.spike.v1_1 import SpikeV11Strategy

        strategy = SpikeV11Strategy(
            "AKEUSDT",
            total_notional=Decimal("1000"),
            rise_5s_threshold=Decimal("0.03"),
            max_rise_5s_percent=Decimal("8"),
            max_volume_multiple_5s=Decimal("15"),
            prior_high_tolerance_percent=Decimal("5"),
        )

        assert strategy.rise_5s_threshold == Decimal("0.03")
        assert strategy.max_rise_5s == Decimal("0.08")
        assert strategy.max_volume_multiple_5s == Decimal("15")
        assert strategy.prior_high_tolerance_percent == Decimal("5")

    def test_v11_upper_limits_allow_zero_to_disable(self):
        from trading_platform.strategies.spike.v1_1 import SpikeV11Strategy

        strategy = SpikeV11Strategy(
            "AKEUSDT",
            total_notional=Decimal("1000"),
            max_rise_5s_percent=Decimal("0"),
            max_volume_multiple_5s=Decimal("0"),
        )

        assert strategy.max_rise_5s is None
        assert strategy.max_volume_multiple_5s is None

    def test_v11_rejects_volume_cap_below_the_lower_threshold(self):
        from trading_platform.strategies.spike.v1_1 import SpikeV11Strategy

        with pytest.raises(ValueError, match="lower volume threshold"):
            SpikeV11Strategy(
                "AKEUSDT",
                total_notional=Decimal("1000"),
                max_volume_multiple_5s=Decimal("2.9"),
            )

    @pytest.mark.parametrize(
        ("low_age_minutes", "signal_expected"),
        [(7 * 24 * 60, True), (60, False)],
    )
    def test_signal_requires_recent_window_low_to_be_at_least_minimum_age(
        self, low_age_minutes, signal_expected
    ):
        minute = 60_000
        lookback_minutes = 7 * 24 * 60
        minute_start = lookback_minutes * minute
        strategy = DynamicSpikeShortStrategy(
            "BTCUSDT",
            total_notional=Decimal("1000"),
            prior_high_lookback_minutes=6 * 60,
            rise_low_lookback_minutes=lookback_minutes,
            min_rise_duration_minutes=24 * 60,
        )
        low_open_time = minute_start - low_age_minutes * minute
        strategy.klines_1m = [
            Kline(
                symbol="BTCUSDT",
                interval="1m",
                open_time=index * minute,
                close_time=(index + 1) * minute - 1,
                available_time=(index + 1) * minute,
                open=Decimal("100"),
                high=Decimal("102"),
                low=Decimal("80") if index * minute == low_open_time else Decimal("85"),
                close=Decimal("100"),
                volume=Decimal("1"),
            )
            for index in range(lookback_minutes)
        ]
        strategy.klines_5m = [
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
                volume=Decimal("1"),
            )
            for index in range(15)
        ]
        closes = [Decimal("100")] * 56 + [
            Decimal("100"), Decimal("101"), Decimal("102"),
            Decimal("104"), Decimal("106"),
        ]
        strategy.bars_1s = [
            Bar1s(
                symbol="BTCUSDT",
                timestamp=minute_start - (60 - index) * 1_000,
                available_time=minute_start - (59 - index) * 1_000,
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

        signal = strategy._detect_signal(strategy.bars_1s[-1])

        assert (signal is not None) is signal_expected
        if signal is not None:
            assert signal.rise_low == Decimal("80")
            assert signal.rise_low_time == low_open_time
            assert signal.rise_low_age_minutes == low_age_minutes

    def test_orders_activate_after_one_second_with_full_ttl(self):
        strategy = DynamicSpikeShortStrategy(
            "BTCUSDT", total_notional=Decimal("1000")
        )
        signal = SpikeSignal(
            signal_time=1_000,
            trigger_price=Decimal("100"),
            spike_high=Decimal("120"),
            origin_price=Decimal("90"),
            atr=Decimal("10"),
            tier_prices=[Decimal("108.5"), Decimal("112.5"), Decimal("116.5")],
            tier_weights=list(strategy.TIER_WEIGHTS),
            invalid_price=Decimal("155"),
            active_time=2_000,
            expire_time=182_000,
        )
        strategy.active_signals.append(signal)

        def bar_at(timestamp: int) -> Bar1s:
            return Bar1s(
                symbol="BTCUSDT",
                timestamp=timestamp,
                available_time=timestamp + 1_000,
                open=Decimal("100"),
                high=Decimal("101"),
                low=Decimal("99"),
                close=Decimal("100"),
                volume=Decimal("1"),
                trade_count=1,
                vwap=Decimal("100"),
            )

        assert strategy._manage_signals(bar_at(1_000)) == []
        intents = strategy._manage_signals(bar_at(2_000))
        assert len(intents) == 3
        assert all(intent.ttl_ms == 180_000 for intent in intents)

    def test_tier3_only_places_one_full_notional_order_at_third_price(self):
        strategy = DynamicSpikeShortStrategy(
            "BTCUSDT",
            total_notional=Decimal("1000"),
            entry_tier_mode="tier3-only",
        )
        signal = SpikeSignal(
            signal_time=1_000,
            trigger_price=Decimal("100"),
            spike_high=Decimal("120"),
            origin_price=Decimal("90"),
            atr=Decimal("10"),
            tier_prices=[Decimal("108.5"), Decimal("112.5"), Decimal("116.5")],
            tier_weights=[Decimal("0"), Decimal("0"), Decimal("1")],
            invalid_price=Decimal("155"),
            active_time=2_000,
            expire_time=182_000,
        )
        strategy.active_signals.append(signal)
        bar = Bar1s(
            symbol="BTCUSDT",
            timestamp=2_000,
            available_time=3_000,
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100"),
            volume=Decimal("1"),
            trade_count=1,
            vwap=Decimal("100"),
        )

        intents = strategy._manage_signals(bar)

        assert len(intents) == 1
        assert intents[0].price == Decimal("116.5")
        assert intents[0].quantity * intents[0].price == Decimal("1000")
        assert intents[0].trigger_reason == "spike_tier3"
        assert intents[0].client_order_id.endswith("_e3")

    def test_single_entry_places_full_notional_at_fixed_original_third_price(self):
        strategy = DynamicSpikeShortStrategy(
            "BTCUSDT",
            total_notional=Decimal("1000"),
            entry_tier_mode="single-entry",
        )
        signal = SpikeSignal(
            signal_time=1_000,
            trigger_price=Decimal("100"),
            spike_high=Decimal("120"),
            origin_price=Decimal("90"),
            atr=Decimal("10"),
            # 保留历史三档快照；新模式的唯一入场价不受旧档位偏移影响。
            tier_prices=[Decimal("108.5"), Decimal("114.5"), Decimal("118.5")],
            tier_weights=list(strategy.TIER_WEIGHTS),
            invalid_price=Decimal("155"),
            active_time=2_000,
            expire_time=182_000,
        )
        strategy.active_signals.append(signal)

        def bar_at(timestamp: int) -> Bar1s:
            return Bar1s(
                symbol="BTCUSDT",
                timestamp=timestamp,
                available_time=timestamp + 1_000,
                open=Decimal("100"),
                high=Decimal("101"),
                low=Decimal("99"),
                close=Decimal("100"),
                volume=Decimal("1"),
                trade_count=1,
                vwap=Decimal("100"),
            )

        assert strategy._manage_signals(bar_at(1_000)) == []
        intents = strategy._manage_signals(bar_at(2_000))

        assert len(intents) == 1
        assert intents[0].price == Decimal("116.50")
        assert intents[0].quantity * intents[0].price == Decimal("1000")
        assert intents[0].trigger_reason == "spike_entry"
        assert intents[0].client_order_id.endswith("_e3")
        assert intents[0].ttl_ms == 180_000
        assert strategy._manage_signals(bar_at(3_000)) == []

    def test_signal_invalidation_cancels_through_account_interface(self):
        class FakeAccount:
            def __init__(self):
                self.cancelled = []
                self.order = Order(
                    order_id="order-1",
                    client_order_id="spike_short_BTCUSDT_1000_tier1",
                    account_id="backtest",
                    symbol="BTCUSDT",
                    side="SELL",
                    type="LIMIT",
                    price=Decimal("108.5"),
                    quantity=Decimal("1"),
                    status="NEW",
                    created_at=2_000,
                    strategy_id="spike_short",
                )

            def get_order(self, order_id):
                return self.order if order_id == self.order.order_id else None

            def iter_orders(self):
                return (self.order,)

            def has_open_position(self, symbol):
                return False

            def cancel_order(self, order_id):
                self.cancelled.append(order_id)
                self.order.status = "CANCELLED"
                return True

        account = FakeAccount()
        strategy = DynamicSpikeShortStrategy(
            "BTCUSDT", total_notional=Decimal("1000"), account=account
        )
        signal = SpikeSignal(
            signal_time=1_000,
            trigger_price=Decimal("100"),
            spike_high=Decimal("120"),
            origin_price=Decimal("90"),
            atr=Decimal("10"),
            tier_prices=[Decimal("108.5"), Decimal("112.5"), Decimal("116.5")],
            tier_weights=list(strategy.TIER_WEIGHTS),
            invalid_price=Decimal("125"),
            active_time=2_000,
            expire_time=182_000,
            placed_client_order_ids={account.order.client_order_id},
        )
        strategy.active_signals.append(signal)
        bar = Bar1s(
            symbol="BTCUSDT", timestamp=3_000, available_time=4_000,
            open=Decimal("124"), high=Decimal("126"), low=Decimal("123"),
            close=Decimal("125"), volume=Decimal("1"), trade_count=1,
            vwap=Decimal("125"),
        )

        assert strategy._manage_signals(bar) == []
        assert account.cancelled == ["order-1"]
        audit = strategy.drain_audit_events()
        assert [event.event_type for event in audit] == ["signal_invalidated"]
        assert audit[0].details == {"cancelled_orders": 1}

    def test_signal_invalidation_cancels_partially_filled_entry_order(self):
        class FakeAccount:
            def __init__(self):
                self.cancelled = []
                self.order = Order(
                    order_id="order-1",
                    client_order_id="spike_short_BTCUSDT_1000_tier1",
                    account_id="backtest",
                    symbol="BTCUSDT",
                    side="SELL",
                    type="LIMIT",
                    price=Decimal("108.5"),
                    quantity=Decimal("1"),
                    status="PARTIALLY_FILLED",
                    created_at=2_000,
                    strategy_id="spike_short",
                )

            def iter_orders(self):
                return (self.order,)

            def cancel_order(self, order_id):
                self.cancelled.append(order_id)
                self.order.status = "CANCELLED"
                return True

        account = FakeAccount()
        strategy = DynamicSpikeShortStrategy(
            "BTCUSDT", total_notional=Decimal("1000"), account=account
        )
        signal = SpikeSignal(
            signal_time=1_000,
            trigger_price=Decimal("100"),
            spike_high=Decimal("120"),
            origin_price=Decimal("90"),
            atr=Decimal("10"),
            tier_prices=[Decimal("108.5"), Decimal("112.5"), Decimal("116.5")],
            tier_weights=list(strategy.TIER_WEIGHTS),
            invalid_price=Decimal("125"),
            active_time=2_000,
            expire_time=182_000,
            placed_client_order_ids={account.order.client_order_id},
        )
        strategy.active_signals.append(signal)
        bar = Bar1s(
            symbol="BTCUSDT", timestamp=3_000, available_time=4_000,
            open=Decimal("124"), high=Decimal("126"), low=Decimal("123"),
            close=Decimal("125"), volume=Decimal("1"), trade_count=1,
            vwap=Decimal("125"),
        )

        assert strategy._manage_signals(bar) == []
        assert account.cancelled == ["order-1"]
        assert strategy.drain_audit_events()[0].details == {"cancelled_orders": 1}

    def test_signal_expiration_cancels_new_and_partially_filled_entry_orders(self):
        class FakeAccount:
            def __init__(self):
                self.cancelled = []
                self.orders = tuple(
                    Order(
                        order_id=f"order-{tier}",
                        client_order_id=f"spike_short_BTCUSDT_1000_tier{tier}",
                        account_id="backtest",
                        symbol="BTCUSDT",
                        side="SELL",
                        type="LIMIT",
                        price=Decimal("108.5"),
                        quantity=Decimal("1"),
                        status=status,
                        created_at=2_000,
                        strategy_id="spike_short",
                    )
                    for tier, status in ((1, "NEW"), (2, "PARTIALLY_FILLED"))
                )

            def iter_orders(self):
                return self.orders

            def cancel_order(self, order_id):
                self.cancelled.append(order_id)
                return True

        account = FakeAccount()
        strategy = DynamicSpikeShortStrategy(
            "BTCUSDT", total_notional=Decimal("1000"), account=account
        )
        signal = SpikeSignal(
            signal_time=1_000,
            trigger_price=Decimal("100"),
            spike_high=Decimal("120"),
            origin_price=Decimal("90"),
            atr=Decimal("10"),
            tier_prices=[Decimal("108.5"), Decimal("112.5"), Decimal("116.5")],
            tier_weights=list(strategy.TIER_WEIGHTS),
            invalid_price=Decimal("155"),
            active_time=2_000,
            expire_time=182_000,
            placed_client_order_ids={
                order.client_order_id for order in account.orders
            },
        )
        strategy.active_signals.append(signal)
        bar = Bar1s(
            symbol="BTCUSDT", timestamp=182_000, available_time=183_000,
            open=Decimal("100"), high=Decimal("101"), low=Decimal("99"),
            close=Decimal("100"), volume=Decimal("1"), trade_count=1,
            vwap=Decimal("100"),
        )

        assert strategy._manage_signals(bar) == []
        assert account.cancelled == ["order-1", "order-2"]
        audit = strategy.drain_audit_events()
        assert [event.event_type for event in audit] == ["signal_expired"]
        assert audit[0].details == {"cancelled_orders": 2}

    def test_d007_non_positive_position_exits_at_900_seconds(self):
        class Account:
            def __init__(self):
                self.position = Position(
                    symbol="BTCUSDT", side="SHORT", entry_price=Decimal("100"),
                    quantity=Decimal("1"), total_commission=Decimal("0.2"),
                    unrealized_pnl=Decimal("0"), realized_pnl=Decimal("0"),
                    opened_at=1_000,
                )

            def get_position(self, symbol):
                return self.position if symbol == "BTCUSDT" else None

        account = Account()
        strategy = DynamicSpikeShortStrategy(
            "BTCUSDT", total_notional=Decimal("1000"), account=account
        )
        strategy.first_fill_time = 1_000
        strategy._campaign_id_for_timing = "spike_short:BTCUSDT:1"
        bar = Bar1s(
            symbol="BTCUSDT", timestamp=901_000, available_time=901_000,
            open=Decimal("110"), high=Decimal("111"), low=Decimal("109"),
            close=Decimal("110"), volume=Decimal("1"), trade_count=1,
            vwap=Decimal("110"),
        )

        intents = strategy._manage_non_positive_timeout(bar)
        assert len(intents) == 1
        assert intents[0].order_type == "MARKET"
        assert intents[0].side == "BUY"
        assert intents[0].quantity == Decimal("1")
        assert [event.event_type for event in strategy.drain_audit_events()] == [
            "campaign_timeout_check", "campaign_timeout_exit_requested"
        ]
        assert strategy._manage_non_positive_timeout(bar) == []

    def test_d007_profitable_position_is_not_force_closed(self):
        class Account:
            def get_position(self, symbol):
                return Position(
                    symbol=symbol, side="SHORT", entry_price=Decimal("100"),
                    quantity=Decimal("1"), total_commission=Decimal("0.2"),
                    unrealized_pnl=Decimal("0"), realized_pnl=Decimal("0"),
                    opened_at=1_000,
                )

        strategy = DynamicSpikeShortStrategy(
            "BTCUSDT", total_notional=Decimal("1000"), account=Account()
        )
        strategy.first_fill_time = 1_000
        strategy._campaign_id_for_timing = "spike_short:BTCUSDT:1"
        bar = Bar1s(
            symbol="BTCUSDT", timestamp=901_000, available_time=901_000,
            open=Decimal("90"), high=Decimal("91"), low=Decimal("89"),
            close=Decimal("90"), volume=Decimal("1"), trade_count=1,
            vwap=Decimal("90"),
        )
        assert strategy._manage_non_positive_timeout(bar) == []
        assert strategy._timeout_checked is True
        assert strategy.drain_audit_events()[0].details["exit_required"] is False

    def test_d009_profitable_old_campaign_is_closed_before_rotation(self):
        class Account:
            def __init__(self):
                self.position = Position(
                    symbol="BTCUSDT", side="SHORT", entry_price=Decimal("100"),
                    quantity=Decimal("1"), total_commission=Decimal("0.2"),
                    unrealized_pnl=Decimal("0"), realized_pnl=Decimal("0"),
                    opened_at=1_000,
                )

            def get_position(self, symbol):
                return self.position if symbol == "BTCUSDT" else None

            def has_open_position(self, symbol):
                return self.get_position(symbol) is not None

            def iter_orders(self):
                return ()

            def cancel_order(self, order_id):
                return True

        account = Account()
        strategy = DynamicSpikeShortStrategy(
            "BTCUSDT", total_notional=Decimal("1000"), account=account
        )
        strategy.first_fill_time = 1_000
        strategy._campaign_id_for_timing = "spike_short:BTCUSDT:1"
        signal = SpikeSignal(
            signal_time=902_000,
            trigger_price=Decimal("110"),
            spike_high=Decimal("120"),
            origin_price=Decimal("90"),
            atr=Decimal("10"),
            tier_prices=[Decimal("108.5"), Decimal("112.5"), Decimal("116.5")],
            tier_weights=list(strategy.TIER_WEIGHTS),
            invalid_price=Decimal("155"),
            active_time=903_000,
            expire_time=1_083_000,
        )
        strategy.active_signals.append(signal)
        bar = Bar1s(
            symbol="BTCUSDT", timestamp=902_000, available_time=902_000,
            open=Decimal("90"), high=Decimal("91"), low=Decimal("89"),
            close=Decimal("90"), volume=Decimal("1"), trade_count=1,
            vwap=Decimal("90"),
        )

        rotation = SpikeSignal(
            signal_time=903_000,
            trigger_price=Decimal("111"),
            spike_high=Decimal("121"),
            origin_price=Decimal("90"),
            atr=Decimal("10"),
            tier_prices=[Decimal("109"), Decimal("113"), Decimal("117")],
            tier_weights=list(strategy.TIER_WEIGHTS),
            invalid_price=Decimal("156"),
            active_time=904_000,
            expire_time=1_084_000,
        )

        intents = strategy._prepare_rotation(rotation, bar)

        assert len(intents) == 1
        assert intents[0].trigger_reason == "campaign_rotation_exit"
        assert intents[0].side == "BUY"
        assert strategy._pending_rotation is rotation
        assert strategy.active_signals == []

        account.position = None
        strategy.set_entry_enabled(False)
        for timestamp in range(843_000, 903_000, 1_000):
            strategy._update_cache(Bar1s(
                symbol="BTCUSDT", timestamp=timestamp,
                available_time=timestamp + 1_000,
                open=Decimal("90"), high=Decimal("91"), low=Decimal("89"),
                close=Decimal("90"), volume=Decimal("1"), trade_count=1,
                vwap=Decimal("90"),
            ))
        strategy.on_bar1s(bar)

        assert strategy._pending_rotation is None
        assert strategy.active_signals == [rotation]
        assert strategy.first_fill_time is None

    def test_minute_window_requires_every_completed_kline(self):
        strategy = DynamicSpikeShortStrategy(
            "BTCUSDT", total_notional=Decimal("1000")
        )

        def kline_at(open_time: int) -> Kline:
            return Kline(
                symbol="BTCUSDT", interval="1m", open_time=open_time,
                close_time=open_time + 59_999,
                available_time=open_time + 60_000,
                open=Decimal("100"), high=Decimal("101"),
                low=Decimal("99"), close=Decimal("100"), volume=Decimal("1"),
            )

        minute_start = 180_000
        strategy.klines_1m = [kline_at(0), kline_at(120_000)]
        assert strategy._min_low_1m(minute_start, 3) is None

        strategy.klines_1m.insert(1, kline_at(60_000))
        assert strategy._min_low_1m(minute_start, 3) == Decimal("99")

    def test_atr_rejects_a_five_minute_gap(self):
        strategy = DynamicSpikeShortStrategy(
            "BTCUSDT", total_notional=Decimal("1000")
        )
        for index in range(strategy.ATR_PERIOD + 1):
            open_time = index * 300_000
            if index == 8:
                open_time += 300_000
            strategy.klines_5m.append(
                Kline(
                    symbol="BTCUSDT", interval="5m", open_time=open_time,
                    close_time=open_time + 299_999,
                    available_time=open_time + 300_000,
                    open=Decimal("100"), high=Decimal("102"),
                    low=Decimal("98"), close=Decimal("101"), volume=Decimal("1"),
                )
            )

        assert strategy._atr_5m() is None

    def test_multi_symbol_adapter_allows_only_one_live_campaign(self):
        class FakeStrategy:
            def __init__(self):
                self.active_signals = []
                self.entry_enabled = True
                self.should_trigger = True
                self.received = 0

            def set_entry_enabled(self, enabled):
                self.entry_enabled = enabled

            def on_bar1s(self, bar):
                self.received += 1
                if self.entry_enabled and self.should_trigger:
                    self.active_signals.append(object())
                    self.should_trigger = False
                return []

            def reset_campaign_timing(self):
                pass

        adapter = DynamicSpikeBacktestStrategy(
            ["AAAUSDT", "BBBUSDT"], Decimal("1000")
        )
        first = FakeStrategy()
        second = FakeStrategy()
        adapter.strategies = {"AAAUSDT": first, "BBBUSDT": second}

        def bar(symbol):
            return Bar1s(
                symbol=symbol, timestamp=1_000, available_time=2_000,
                open=Decimal("100"), high=Decimal("100"), low=Decimal("100"),
                close=Decimal("100"), volume=Decimal("1"), trade_count=1,
                vwap=Decimal("100"),
            )

        adapter.on_bar1s(bar("AAAUSDT"))
        adapter.on_bar1s(bar("BBBUSDT"))
        assert adapter.active_symbol == "AAAUSDT"
        assert second.received == 1
        assert second.active_signals == []

        first.active_signals.clear()
        adapter.on_bar1s(bar("AAAUSDT"))
        adapter.on_bar1s(bar("BBBUSDT"))
        assert adapter.active_symbol == "BBBUSDT"
        assert len(second.active_signals) == 1

    def test_multi_symbol_adapter_keeps_campaign_for_partially_filled_entry(self):
        class FakeAccount:
            def __init__(self):
                self.order = Order(
                    order_id="order-1",
                    client_order_id="spike_short_AAAUSDT_1000_tier1",
                    account_id="backtest",
                    symbol="AAAUSDT",
                    side="SELL",
                    type="LIMIT",
                    price=Decimal("100"),
                    quantity=Decimal("1"),
                    status="PARTIALLY_FILLED",
                    created_at=1_000,
                    strategy_id="spike_short",
                )

            def has_open_position(self, symbol):
                return False

            def iter_orders(self):
                return (self.order,)

        class FakeStrategy:
            def __init__(self, should_trigger):
                self.active_signals = []
                self.entry_enabled = True
                self.should_trigger = should_trigger

            def set_entry_enabled(self, enabled):
                self.entry_enabled = enabled

            def on_bar1s(self, bar):
                if self.entry_enabled and self.should_trigger:
                    self.active_signals.append(object())
                return []

            def reset_campaign_timing(self):
                pass

        account = FakeAccount()
        adapter = DynamicSpikeBacktestStrategy(
            ["AAAUSDT", "BBBUSDT"], Decimal("1000"), account=account
        )
        first = FakeStrategy(should_trigger=False)
        second = FakeStrategy(should_trigger=True)
        adapter.strategies = {"AAAUSDT": first, "BBBUSDT": second}
        adapter.active_symbol = "AAAUSDT"

        def bar(symbol):
            return Bar1s(
                symbol=symbol, timestamp=1_000, available_time=2_000,
                open=Decimal("100"), high=Decimal("100"), low=Decimal("100"),
                close=Decimal("100"), volume=Decimal("1"), trade_count=1,
                vwap=Decimal("100"),
            )

        adapter.on_bar1s(bar("AAAUSDT"))
        adapter.on_bar1s(bar("BBBUSDT"))

        assert adapter.active_symbol == "AAAUSDT"
        assert second.entry_enabled is False
        assert second.active_signals == []

    def test_multi_symbol_adapter_exposes_global_entry_gate(self):
        adapter = DynamicSpikeBacktestStrategy(
            ["AAAUSDT", "BBBUSDT"], Decimal("1000")
        )
        adapter.set_entry_enabled(False)
        assert all(
            strategy._entry_enabled is False
            for strategy in adapter.strategies.values()
        )
        adapter.set_entry_enabled(True)
        assert all(
            strategy._entry_enabled is True
            for strategy in adapter.strategies.values()
        )

    def test_multi_symbol_adapter_does_not_override_closed_global_entry_gate(self):
        class FakeStrategy:
            def __init__(self):
                self.active_signals = []
                self.entry_enabled = True

            def set_entry_enabled(self, enabled):
                self.entry_enabled = enabled

            def on_bar1s(self, bar):
                if self.entry_enabled:
                    self.active_signals.append(object())
                return []

        adapter = DynamicSpikeBacktestStrategy(["AAAUSDT"], Decimal("1000"))
        fake = FakeStrategy()
        adapter.strategies = {"AAAUSDT": fake}
        adapter.set_entry_enabled(False)
        bar = Bar1s(
            symbol="AAAUSDT", timestamp=1_000, available_time=2_000,
            open=Decimal("100"), high=Decimal("100"), low=Decimal("100"),
            close=Decimal("100"), volume=Decimal("1"), trade_count=1,
            vwap=Decimal("100"),
        )

        adapter.on_bar1s(bar)

        assert fake.entry_enabled is False
        assert fake.active_signals == []


class TestV21GroupedConsecutiveFilter:
    """测试 V21 按动能分组的连阳双标准过滤。"""

    def _strategy(self, **kwargs):
        return SpikeV21Strategy(
            "BTCUSDT",
            total_notional=Decimal("1000"),
            **kwargs,
        )

    def _klines_consecutive_up(self, minutes: int) -> list[Kline]:
        return [
            Kline(
                symbol="BTCUSDT",
                interval="1m",
                open_time=index * 60_000,
                close_time=(index + 1) * 60_000 - 1,
                available_time=(index + 1) * 60_000,
                open=Decimal("1"),
                high=Decimal("2"),
                low=Decimal("1"),
                close=Decimal("2"),
                volume=Decimal("1"),
            )
            for index in range(minutes)
        ]

    def test_grouped_strong_bucket_uses_strict_cap(self):
        strategy = self._strategy(
            max_consecutive_up_minutes=4,
            group_rise_12h_threshold=1.0,
            loose_consecutive_up_minutes=0,
        )
        strategy.klines_1m = self._klines_consecutive_up(6)

        passed, details = strategy._entry_filter_decision(
            6 * 60_000, rise_from_12h_low=Decimal("1.23")
        )

        assert passed is False
        assert details["bucket"] == "strong"
        assert details["max_consecutive_up_minutes"] == 4
        assert details["consecutive_up_minutes"] == 6
        assert details["rise_from_12h_low"] == "1.23"

    def test_grouped_weak_bucket_is_fully_loosened(self):
        strategy = self._strategy(
            max_consecutive_up_minutes=4,
            group_rise_12h_threshold=1.0,
            loose_consecutive_up_minutes=0,
        )
        strategy.klines_1m = self._klines_consecutive_up(6)

        passed, details = strategy._entry_filter_decision(
            6 * 60_000, rise_from_12h_low=Decimal("0.27")
        )

        assert passed is True
        assert details is None

    def test_grouped_weak_bucket_with_loose_cap(self):
        strategy = self._strategy(
            max_consecutive_up_minutes=4,
            group_rise_12h_threshold=1.0,
            loose_consecutive_up_minutes=10,
        )
        strategy.klines_1m = self._klines_consecutive_up(12)

        passed, details = strategy._entry_filter_decision(
            12 * 60_000, rise_from_12h_low=Decimal("0.27")
        )

        assert passed is False
        assert details["bucket"] == "weak"
        assert details["max_consecutive_up_minutes"] == 10
        assert details["consecutive_up_minutes"] == 12

    def test_grouped_weak_bucket_passes_within_loose_cap(self):
        strategy = self._strategy(
            max_consecutive_up_minutes=4,
            group_rise_12h_threshold=1.0,
            loose_consecutive_up_minutes=10,
        )
        strategy.klines_1m = self._klines_consecutive_up(8)

        passed, details = strategy._entry_filter_decision(
            8 * 60_000, rise_from_12h_low=Decimal("0.27")
        )

        assert passed is True
        assert details is None

    def test_grouped_threshold_boundary_counts_as_strong(self):
        strategy = self._strategy(
            max_consecutive_up_minutes=4,
            group_rise_12h_threshold=1.0,
            loose_consecutive_up_minutes=0,
        )
        strategy.klines_1m = self._klines_consecutive_up(5)

        passed, details = strategy._entry_filter_decision(
            5 * 60_000, rise_from_12h_low=Decimal("1.0")
        )

        assert passed is False
        assert details["bucket"] == "strong"

    def test_grouped_disabled_matches_single_cap_audit_shape(self):
        strategy = self._strategy(max_consecutive_up_minutes=3)
        strategy.klines_1m = self._klines_consecutive_up(4)

        passed, details = strategy._entry_filter_decision(4 * 60_000)

        assert passed is False
        assert details == {
            "rejection_stage": "consecutive_up_entry_filter",
            "rejection_reasons": ["max_consecutive_up_minutes"],
            "consecutive_up_minutes": 4,
            "max_consecutive_up_minutes": 3,
        }

    def test_grouped_missing_rise_snapshot_falls_back_to_weak(self):
        strategy = self._strategy(
            max_consecutive_up_minutes=4,
            group_rise_12h_threshold=1.0,
            loose_consecutive_up_minutes=0,
        )
        strategy.klines_1m = self._klines_consecutive_up(6)

        passed, details = strategy._entry_filter_decision(6 * 60_000, None)

        assert passed is True
        assert details is None

    def test_grouped_negative_parameter_is_rejected(self):
        with pytest.raises(ValueError, match="group_rise_12h_threshold"):
            self._strategy(
                max_consecutive_up_minutes=4,
                group_rise_12h_threshold=-1.0,
            )
        with pytest.raises(ValueError, match="loose_consecutive_up_minutes"):
            self._strategy(
                max_consecutive_up_minutes=4,
                loose_consecutive_up_minutes=-1,
            )
        with pytest.raises(ValueError, match="loose_max_ls_ratio"):
            self._strategy(
                max_consecutive_up_minutes=4,
                loose_max_ls_ratio=-0.5,
            )

    def test_grouped_weak_bucket_loosens_ls_ratio(self):
        strategy = self._strategy(
            max_ls_ratio=1.5,
            group_rise_12h_threshold=1.0,
            loose_max_ls_ratio=0,
        )
        strategy.metrics_series = [
            (0, 100.0, 1.0),
            (4 * 60_000, 100.0, 5.9),
        ]

        passed, details = strategy._entry_filter_decision(
            4 * 60_000, rise_from_12h_low=Decimal("0.27")
        )

        assert passed is True
        assert details is None

    def test_grouped_strong_bucket_keeps_strict_ls_ratio(self):
        strategy = self._strategy(
            max_ls_ratio=1.5,
            group_rise_12h_threshold=1.0,
            loose_max_ls_ratio=0,
        )
        strategy.metrics_series = [
            (0, 100.0, 1.0),
            (4 * 60_000, 100.0, 5.9),
        ]

        passed, details = strategy._entry_filter_decision(
            4 * 60_000, rise_from_12h_low=Decimal("1.23")
        )

        assert passed is False
        assert details["rejection_stage"] == "metrics_entry_filters"
        assert details["rejection_reasons"] == ["max_ls_ratio"]
        assert details["max_ls_ratio"] == 1.5

    def test_grouped_weak_ls_audit_exposes_bucket(self):
        strategy = self._strategy(
            max_ls_ratio=1.5,
            group_rise_12h_threshold=1.0,
            loose_max_ls_ratio=0,
            max_consecutive_up_minutes=0,
        )
        strategy.metrics_series = [
            (0, 100.0, 1.0),
            (4 * 60_000, 100.0, 1.0),
        ]
        strategy.klines_1m = self._klines_consecutive_up(6)

        passed, details = strategy._entry_filter_decision(
            4 * 60_000, rise_from_12h_low=Decimal("0.27")
        )

        assert passed is True
        assert details is None

    def test_loose_ls_disabled_matches_original_audit_shape(self):
        strategy = self._strategy(
            max_ls_ratio=1.5,
            group_rise_12h_threshold=1.0,
            loose_max_ls_ratio=None,
            max_consecutive_up_minutes=0,
        )
        strategy.metrics_series = [
            (0, 100.0, 1.0),
            (4 * 60_000, 100.0, 5.9),
        ]

        passed, details = strategy._entry_filter_decision(
            4 * 60_000, rise_from_12h_low=Decimal("0.27")
        )

        assert passed is False
        assert details == {
            "rejection_stage": "metrics_entry_filters",
            "rejection_reasons": ["max_ls_ratio"],
            "oi": 100.0,
            "previous_oi": 100.0,
            "oi_change_pct": 0.0,
            "ls_ratio": 5.9,
            "metrics_available_time": 4 * 60_000,
            "max_oi_change_pct": 0.0,
            "max_ls_ratio": 1.5,
        }

    def test_grouped_weak_ls_loose_audit_on_rejection(self):
        strategy = self._strategy(
            max_ls_ratio=1.5,
            group_rise_12h_threshold=1.0,
            loose_max_ls_ratio=2.0,
            max_consecutive_up_minutes=0,
        )
        strategy.metrics_series = [
            (0, 100.0, 1.0),
            (4 * 60_000, 100.0, 5.9),
        ]

        passed, details = strategy._entry_filter_decision(
            4 * 60_000, rise_from_12h_low=Decimal("0.27")
        )

        assert passed is False
        assert details["max_ls_ratio"] == 2.0
        assert details["bucket"] == "weak"
        assert details["loose_max_ls_ratio"] == 2.0

    def test_strong_tier_atr_shift_applies_to_strong_bucket(self):
        strategy = self._strategy(
            group_rise_12h_threshold=1.0,
            strong_tier_atr_shift=0.20,
        )
        shift = strategy._entry_tier_atr_shift(Decimal("1.23"))
        assert shift == Decimal("0.20")

    def test_strong_tier_atr_shift_ignores_weak_bucket(self):
        strategy = self._strategy(
            group_rise_12h_threshold=1.0,
            strong_tier_atr_shift=0.20,
        )
        shift = strategy._entry_tier_atr_shift(Decimal("0.27"))
        assert shift == Decimal("0")

    def test_strong_tier_atr_shift_zero_disabled(self):
        strategy = self._strategy(
            group_rise_12h_threshold=1.0,
            strong_tier_atr_shift=0,
        )
        shift = strategy._entry_tier_atr_shift(Decimal("1.23"))
        assert shift == Decimal("0")

    def test_strong_tier_atr_shift_base_strategy_returns_zero(self):
        strategy = DynamicSpikeShortStrategy(
            "BTCUSDT", total_notional=Decimal("1000")
        )
        shift = strategy._entry_tier_atr_shift(Decimal("1.23"))
        assert shift == Decimal("0")

    def test_strong_tier_atr_shift_negative_is_rejected(self):
        with pytest.raises(ValueError, match="strong_tier_atr_shift"):
            self._strategy(
                group_rise_12h_threshold=1.0,
                strong_tier_atr_shift=-0.1,
            )

    def _strong_bucket_signal(self, shift: Decimal) -> SpikeSignal:
        minute = 60_000
        lookback_minutes = 7 * 24 * 60
        minute_start = lookback_minutes * minute
        low_open_time = minute_start - lookback_minutes * minute
        strategy = SpikeV21Strategy(
            "BTCUSDT",
            total_notional=Decimal("1000"),
            prior_high_lookback_minutes=6 * 60,
            rise_low_lookback_minutes=lookback_minutes,
            min_rise_duration_minutes=24 * 60,
            group_rise_12h_threshold=Decimal("1.0"),
            strong_tier_atr_shift=shift,
        )
        strategy.klines_1m = [
            Kline(
                symbol="BTCUSDT",
                interval="1m",
                open_time=index * minute,
                close_time=(index + 1) * minute - 1,
                available_time=(index + 1) * minute,
                open=Decimal("100"),
                high=Decimal("102"),
                low=Decimal("80") if index * minute == low_open_time else Decimal("85"),
                close=Decimal("100"),
                volume=Decimal("1"),
            )
            for index in range(lookback_minutes)
        ]
        strategy.klines_5m = [
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
                volume=Decimal("1"),
            )
            for index in range(15)
        ]
        closes = [Decimal("100")] * 56 + [
            Decimal("166"), Decimal("168"), Decimal("170"),
            Decimal("172"), Decimal("174"),
        ]
        strategy.bars_1s = [
            Bar1s(
                symbol="BTCUSDT",
                timestamp=minute_start - (60 - index) * 1_000,
                available_time=minute_start - (59 - index) * 1_000,
                open=close,
                high=Decimal("200") if index == 60 else close,
                low=close,
                close=close,
                volume=Decimal("4") if index >= 56 else Decimal("1"),
                trade_count=1,
                vwap=close,
            )
            for index, close in enumerate(closes)
        ]
        signal = strategy._detect_signal(strategy.bars_1s[-1])
        assert signal is not None
        return signal

    def test_strong_tier_atr_shift_keeps_protection_tier_fixed(self):
        shift0 = self._strong_bucket_signal(Decimal("0"))
        shift02 = self._strong_bucket_signal(Decimal("0.2"))

        assert shift0.rise_from_12h_low >= Decimal("1.0")
        assert shift0.tier_prices[0] == shift02.tier_prices[0]
        assert shift02.tier_prices[1] == shift0.tier_prices[1] + Decimal("0.2") * shift0.atr
        assert shift02.tier_prices[2] == shift0.tier_prices[2] + Decimal("0.2") * shift0.atr

    def test_strong_tier_atr_shift_weak_bucket_tiers_unchanged(self):
        shift0 = self._strong_bucket_signal(Decimal("0"))
        assert shift0.tier_prices[0] < shift0.tier_prices[1] < shift0.tier_prices[2]
        assert all(t < Decimal("200") for t in shift0.tier_prices)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
