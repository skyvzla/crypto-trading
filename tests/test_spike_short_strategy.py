"""
测试Dynamic Spike Short Strategy
"""
import pytest
from decimal import Decimal
from trading_platform.shared.events import Bar1s, Kline
from trading_platform.strategies.spike_short import DynamicSpikeShortStrategy


class TestDynamicSpikeShortStrategy:
    """测试逼空做空策略"""

    def test_strategy_creation(self):
        """测试策略创建"""
        strategy = DynamicSpikeShortStrategy("BTCUSDT")
        assert strategy.symbol == "BTCUSDT"
        assert len(strategy.bars_1s) == 0
        assert len(strategy.klines_1m) == 0
        assert len(strategy.klines_5m) == 0

    def test_cache_update(self):
        """测试缓存更新"""
        strategy = DynamicSpikeShortStrategy("BTCUSDT")

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

        # 应该只保留最近60个
        assert len(strategy.bars_1s) == 60

    def test_kline_update(self):
        """测试K线更新"""
        strategy = DynamicSpikeShortStrategy("BTCUSDT")

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
        strategy = DynamicSpikeShortStrategy("BTCUSDT")

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

        signal = strategy._detect_signal(current_bar, 1609459211000)
        assert signal is None  # 数据不足

    def test_strategy_parameters(self):
        """测试策略参数"""
        assert DynamicSpikeShortStrategy.SPIKE_RISE_5S == Decimal("0.05")
        assert DynamicSpikeShortStrategy.VOLUME_MULTIPLE_5S == Decimal("3.0")
        assert DynamicSpikeShortStrategy.RISE_FROM_12H_LOW == Decimal("0.20")
        assert len(DynamicSpikeShortStrategy.TIER_WEIGHTS) == 3
        assert sum(DynamicSpikeShortStrategy.TIER_WEIGHTS) == Decimal("1.0")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
