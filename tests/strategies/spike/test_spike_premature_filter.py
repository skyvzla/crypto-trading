"""测试 _premature_spike_filter 过早触发过滤（30m 偏离度 + 60m 极差）。"""
from decimal import Decimal

from trading_platform.shared.events import Kline
from trading_platform.strategies.spike.short import DynamicSpikeShortStrategy


def _minute_klines(minute_start, minutes, price_fn):
    """构造连续 1m K；price_fn(open_time) 返回 (open, high, low, close)。"""
    minute = 60_000
    klines = []
    for i in range(minutes):
        t = minute_start - (minutes - i) * minute
        open_p, high_p, low_p, close_p = price_fn(t)
        klines.append(
            Kline(
                symbol="TESTUSDT",
                interval="1m",
                open_time=t,
                close_time=t + minute - 1,
                available_time=t + minute,
                open=Decimal(str(open_p)),
                high=Decimal(str(high_p)),
                low=Decimal(str(low_p)),
                close=Decimal(str(close_p)),
                volume=Decimal("1"),
            )
        )
    return klines


class TestPrematureSpikeFilter:
    def test_horizontal_then_gentle_rise_passes(self):
        """横盘后温和上行触发：偏离度和极差都低，不应被过滤。"""
        minute = 60_000
        minute_start = 10_000 * minute
        # 前 60 分钟全部 close 0.10（横盘），触发价 0.105（偏离 5%）
        strategy = DynamicSpikeShortStrategy(
            "TESTUSDT",
            total_notional=Decimal("1000"),
            spike_avg_deviation_max_pct=28,
            spike_range_max_pct=35,
        )
        strategy.klines_1m = _minute_klines(minute_start, 60, lambda t: (0.10, 0.101, 0.099, 0.10))
        res = strategy._premature_spike_filter(minute_start, Decimal("0.105"))
        assert res is not None
        assert res["rejected"] is False
        assert res["spike_avg_deviation_pct"] < 28
        assert res["spike_range_pct"] < 35

    def test_spike_top_rejected(self):
        """横盘 59 分钟最后一分钟暴拉到高位：偏离度和极差都超阈值，应拒绝。"""
        minute = 60_000
        minute_start = 10_000 * minute
        # 前 59 分钟 0.10；最后一分钟（当前分钟，不算入窗口）价格 0.14 触发
        # 但窗口内的 60 根 1m K 是前 60 分钟：全部 0.10，极差低 → 不会拒绝。
        # 为模拟"窗口内即含 spike"，让窗口内最后一根（信号前 1 分钟）就拉到 0.14。
        def price_fn(t):
            age_min = (minute_start - t) // minute
            if age_min < 2:
                return (0.14, 0.145, 0.135, 0.14)  # 信号前 1-2 分钟暴拉
            return (0.10, 0.101, 0.099, 0.10)

        strategy = DynamicSpikeShortStrategy(
            "TESTUSDT",
            total_notional=Decimal("1000"),
            spike_avg_deviation_max_pct=28,
            spike_range_max_pct=35,
        )
        strategy.klines_1m = _minute_klines(minute_start, 60, price_fn)
        # 触发价 0.14：前 30m 均价 ≈ (0.14+59*0.10)/60=0.1007 → 偏离 39%
        # 极差 = (0.145-0.099)/0.099 = 46% → 双超 → 拒绝
        res = strategy._premature_spike_filter(minute_start, Decimal("0.14"))
        assert res is not None
        assert res["rejected"] is True

    def test_disabled_when_threshold_zero(self):
        """阈值=0 时不调用过滤，_detect_signal 不拦截。"""
        strategy = DynamicSpikeShortStrategy(
            "TESTUSDT", total_notional=Decimal("1000")
        )
        assert strategy.spike_avg_deviation_max_pct == 0
        assert strategy.spike_range_max_pct == 0

    def test_validation_requires_both(self):
        """只设一个阈值必须报错。"""
        import pytest

        with pytest.raises(ValueError):
            DynamicSpikeShortStrategy(
                "TESTUSDT",
                total_notional=Decimal("1000"),
                spike_avg_deviation_max_pct=28,
            )
        with pytest.raises(ValueError):
            DynamicSpikeShortStrategy(
                "TESTUSDT",
                total_notional=Decimal("1000"),
                spike_range_max_pct=35,
            )
        with pytest.raises(ValueError):
            DynamicSpikeShortStrategy(
                "TESTUSDT",
                total_notional=Decimal("1000"),
                spike_avg_deviation_max_pct=-1,
                spike_range_max_pct=35,
            )