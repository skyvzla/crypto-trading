"""
测试Dynamic Spike Short Strategy
"""
import pytest
from decimal import Decimal
from trading_platform.shared.events import Bar1s, Kline
from trading_platform.strategies.spike_short import (
    DynamicSpikeBacktestStrategy,
    DynamicSpikeShortStrategy,
    SpikeSignal,
)


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
        assert len(DynamicSpikeShortStrategy.TIER_WEIGHTS) == 3
        assert sum(DynamicSpikeShortStrategy.TIER_WEIGHTS) == Decimal("1.0")

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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
