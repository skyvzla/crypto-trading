"""测试 _box_breakthrough 箱体/通道突破时长计算。"""
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


class TestBoxBreakthrough:
    def test_horizontal_box_break_sustained(self):
        """横盘箱体，突破已持续 5h：时长达标，应放行。"""
        minute = 60_000
        minute_start = 10_000 * minute
        days7 = 7 * 24 * 60
        # 前 ~6.8d 横盘 close 0.15；最近 5h 突破到 close 0.20
        # （突破跨越多根已完成 1h bar，前 4h 已完成、最后一根含当前小时）
        def price_fn(t):
            age_min = (minute_start - t) // minute
            if age_min < 4 * 60:
                return (0.20, 0.21, 0.19, 0.20)
            if age_min < 5 * 60:
                return (0.20, 0.21, 0.19, 0.20)
            return (0.15, 0.155, 0.145, 0.15)

        strategy = DynamicSpikeShortStrategy(
            "TESTUSDT", total_notional=Decimal("1000")
        )
        strategy.klines_1m = _minute_klines(minute_start, days7, price_fn)
        res = strategy._box_breakthrough(minute_start, Decimal("0.20"))
        assert res is not None
        # 突破时长 = 最近跌破后持续站上：约 5h，>= 4h 阈值
        assert 4 * 60 <= res["box_break_minutes"] <= 6 * 60
        # 突破线上沿应介于横盘高(0.155)与突破价(0.20)之间
        assert Decimal("0.155") <= res["box_breakthrough"] <= Decimal("0.20")

    def test_recent_break_short_duration_filtered(self):
        """刚突破不久（1.5h，跨当前小时）：时长 < 4h，应被过滤。"""
        minute = 60_000
        minute_start = 10_000 * minute
        days7 = 7 * 24 * 60

        def price_fn(t):
            age_min = (minute_start - t) // minute
            if age_min < 90:
                return (0.20, 0.21, 0.19, 0.20)
            return (0.15, 0.155, 0.145, 0.15)

        strategy = DynamicSpikeShortStrategy(
            "TESTUSDT", total_notional=Decimal("1000")
        )
        strategy.klines_1m = _minute_klines(minute_start, days7, price_fn)
        res = strategy._box_breakthrough(minute_start, Decimal("0.20"))
        assert res is not None
        assert res["box_break_minutes"] < 4 * 60

    def test_uptrend_channel_uses_upper_band(self):
        """上升通道突破上轨并持续：上沿用通道上轨（回归线+1.5σ），时长达标。"""
        minute = 60_000
        minute_start = 10_000 * minute
        days7 = 7 * 24 * 60
        total = days7
        base_open = minute_start - total * minute
        # 前 6.8d 上升通道 0.10→0.20；最后 5h 大幅突破上轨至 0.40 并持续
        def price_fn(t):
            idx = (t - base_open) // minute
            age_min = (minute_start - t) // minute
            if age_min < 5 * 60:
                level = 0.40
            else:
                level = 0.10 + 0.20 * idx / total
            return (level, level * 1.005, level * 0.995, level)

        strategy = DynamicSpikeShortStrategy(
            "TESTUSDT", total_notional=Decimal("1000")
        )
        strategy.klines_1m = _minute_klines(minute_start, days7, price_fn)
        res = strategy._box_breakthrough(minute_start, Decimal("0.40"))
        assert res is not None
        # 通道上轨应显著高于纯横盘（0.155），突破时长约 5h >= 4h
        assert res["box_breakthrough"] >= Decimal("0.155")
        assert res["box_break_minutes"] >= 4 * 60

    def test_disabled_when_threshold_zero(self):
        """box_duration_min_minutes=0 时不计算，返回 None 过滤不生效。"""
        strategy = DynamicSpikeShortStrategy(
            "TESTUSDT", total_notional=Decimal("1000")
        )
        # 无参数时 box_duration_min_minutes=0，_build_signal 不会调用 _box_breakthrough
        assert strategy.box_duration_min_minutes == 0