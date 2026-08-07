"""
Trading Platform V1 - 集成测试套件
测试所有核心功能
"""
import pytest
import asyncio
from decimal import Decimal
from datetime import datetime

# 导入所有核心模块
from trading_platform.shared.events import Bar1s, Kline, OrderIntent, Order
from trading_platform.shared.order_states import is_valid_transition, is_terminal_status
from trading_platform.shared.risk import RiskGuard, RiskConfig


class TestEventModels:
    """测试事件模型"""

    def test_bar1s_creation(self):
        """测试Bar1s创建"""
        bar = Bar1s(
            symbol="BTCUSDT",
            timestamp=1609459200000,
            available_time=1609459201000,
            open=Decimal("30000"),
            high=Decimal("30100"),
            low=Decimal("29900"),
            close=Decimal("30050"),
            volume=Decimal("10.5"),
            trade_count=100,
            vwap=Decimal("30025"),
        )
        assert bar.symbol == "BTCUSDT"
        assert bar.close == Decimal("30050")
        assert bar.available_time == bar.timestamp + 1000

    def test_kline_creation(self):
        """测试Kline创建"""
        kline = Kline(
            symbol="ETHUSDT",
            interval="1m",
            open_time=1609459200000,
            close_time=1609459259999,
            available_time=1609459260000,
            open=Decimal("1000"),
            high=Decimal("1010"),
            low=Decimal("990"),
            close=Decimal("1005"),
            volume=Decimal("100"),
        )
        assert kline.symbol == "ETHUSDT"
        assert kline.interval == "1m"
        assert kline.available_time == kline.close_time + 1


class TestOrderStates:
    """测试订单状态机"""

    def test_valid_transitions(self):
        """测试合法状态转换"""
        assert is_valid_transition('NEW', 'FILLED') == True
        assert is_valid_transition('NEW', 'CANCELLED') == True
        assert is_valid_transition('SUBMIT_UNKNOWN', 'NEW') == True
        assert is_valid_transition('SUBMIT_UNKNOWN', 'FILLED') == True
        assert is_valid_transition('SUBMIT_UNKNOWN', 'PARTIALLY_FILLED') == True
        assert is_valid_transition('SUBMIT_UNKNOWN', 'EXPIRED') == True

    def test_invalid_transitions(self):
        """测试非法状态转换"""
        assert is_valid_transition('FILLED', 'NEW') == False
        assert is_valid_transition('CANCELLED', 'NEW') == False
        assert is_valid_transition('NEW', 'SUBMIT_UNKNOWN') == False

    def test_same_state_allowed(self):
        """测试同状态转换（重复推送）"""
        assert is_valid_transition('NEW', 'NEW') == True
        assert is_valid_transition('FILLED', 'FILLED') == True

    def test_terminal_status(self):
        """测试终态判断"""
        assert is_terminal_status('FILLED') == True
        assert is_terminal_status('CANCELLED') == True
        assert is_terminal_status('EXPIRED') == True
        assert is_terminal_status('NEW') == False


class TestRiskGuard:
    """测试风控守卫"""

    def test_risk_guard_creation(self):
        """测试风控守卫创建"""
        config = RiskConfig(
            max_position_value_usdt=Decimal("10000"),
            max_symbols=5,
            max_leverage=3,
        )
        guard = RiskGuard("account_a", config)
        assert guard.account_id == "account_a"
        assert guard.get_total_position_value() == Decimal("0")

    def test_can_open_position(self):
        """测试开仓检查"""
        config = RiskConfig(max_position_value_usdt=Decimal("10000"), max_symbols=3)
        guard = RiskGuard("account_a", config)

        # 第一个币种，可以开仓
        can_open, reason = guard.check_can_open("BTCUSDT", Decimal("3000"))
        assert can_open == True

        # 更新持仓
        guard.update_position("BTCUSDT", Decimal("3000"))

        # 第二个币种，可以开仓
        can_open, reason = guard.check_can_open("ETHUSDT", Decimal("3000"))
        assert can_open == True
        guard.update_position("ETHUSDT", Decimal("3000"))

        # 第三个币种，可以开仓
        can_open, reason = guard.check_can_open("BNBUSDT", Decimal("3000"))
        assert can_open == True
        guard.update_position("BNBUSDT", Decimal("3000"))

        # 第四个币种，超过max_symbols
        can_open, reason = guard.check_can_open("ADAUSDT", Decimal("1000"))
        assert can_open == False
        assert "Max symbols" in reason

    def test_max_position_value(self):
        """测试总持仓价值上限"""
        config = RiskConfig(max_position_value_usdt=Decimal("10000"))
        guard = RiskGuard("account_a", config)

        guard.update_position("BTCUSDT", Decimal("8000"))

        # 尝试开仓超过上限
        can_open, reason = guard.check_can_open("ETHUSDT", Decimal("3000"))
        assert can_open == False
        assert "Max position value" in reason

    def test_rejects_invalid_notional_and_excess_leverage(self):
        guard = RiskGuard(
            "account_a",
            RiskConfig(max_position_value_usdt=Decimal("10000"), max_leverage=3),
        )

        can_open, reason = guard.check_can_open("BTCUSDT", Decimal("0"))
        assert can_open is False
        assert "positive" in reason

        can_open, reason = guard.check_can_open(
            "BTCUSDT", Decimal("1000"), leverage=4
        )
        assert can_open is False
        assert "Leverage" in reason

        can_open, reason = guard.check_can_open(
            "BTCUSDT", Decimal("1000"), leverage=3
        )
        assert can_open is True

    def test_symbol_blocking(self):
        """测试币种阻塞"""
        guard = RiskGuard("account_a", RiskConfig())

        # 阻塞币种
        guard.block_symbol("BTCUSDT", "SUBMIT_UNKNOWN pending")

        # 尝试开仓被阻塞的币种
        can_open, reason = guard.check_can_open("BTCUSDT", Decimal("1000"))
        assert can_open == False
        assert "blocked" in reason.lower()

        # 解除阻塞
        guard.unblock_symbol("BTCUSDT")
        can_open, reason = guard.check_can_open("BTCUSDT", Decimal("1000"))
        assert can_open == True

    def test_halt_rejects_all_new_positions_and_preserves_first_reason(self):
        guard = RiskGuard("account_a", RiskConfig())

        guard.halt("execution report handling failed")
        guard.halt("later account update failure")

        for symbol in ("BTCUSDT", "ETHUSDT"):
            can_open, reason = guard.check_can_open(symbol, Decimal("1000"))
            assert can_open is False
            assert reason == "Risk guard halted: execution report handling failed"
        assert guard.halted is True
        assert guard.halt_reason == "execution report handling failed"


class TestBacktestEngine:
    """测试回测引擎"""

    def test_import_backtest_modules(self):
        """测试回测模块导入"""
        from trading_platform.backtest.loader import BacktestDataLoader
        from trading_platform.backtest.engine import BacktestEngine
        from trading_platform.backtest.executor import BacktestExecutor
        from trading_platform.backtest.result import BacktestResult

        assert BacktestDataLoader is not None
        assert BacktestEngine is not None
        assert BacktestExecutor is not None
        assert BacktestResult is not None

    def test_executor_order_lifecycle(self):
        """测试执行器订单生命周期 - 简化版本"""
        # BacktestExecutor需要BacktestEngine实例，这里只测试导入
        # 完整的生命周期测试在backtest/test_backtest.py中
        from trading_platform.backtest.executor import BacktestExecutor
        assert BacktestExecutor is not None


class TestMarketLayer:
    """测试行情层"""

    def test_import_market_modules(self):
        """测试行情层模块导入"""
        from trading_platform.market.feed.binance_ws import BinanceWebSocketClient
        from trading_platform.market.feed.aggregator import Bar1sAggregator
        from trading_platform.market.store.redis_pub import RedisPublisher
        from trading_platform.market.store.kline_store import KlineStore

        assert BinanceWebSocketClient is not None
        assert Bar1sAggregator is not None
        assert RedisPublisher is not None
        assert KlineStore is not None


class TestStrategyLayer:
    """测试策略层"""

    def test_import_strategy_modules(self):
        """测试策略层模块导入"""
        from trading_platform.strategies.kline.base import KlineStrategyBase
        from trading_platform.strategies.tick.base import TickStrategyBase

        assert KlineStrategyBase is not None
        assert TickStrategyBase is not None


class TestLedgerLayer:
    """测试账本层"""

    def test_import_ledger_modules(self):
        """测试账本层模块导入"""
        from trading_platform.ledger.db.models import Order as OrderModel
        from trading_platform.ledger.api.routes import router

        assert OrderModel is not None
        assert router is not None


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
