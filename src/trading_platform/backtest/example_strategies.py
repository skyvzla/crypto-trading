"""
回测引擎示例策略

演示如何实现符合回测引擎接口的策略。
"""
from decimal import Decimal
import logging

from trading_platform.shared.events import Bar1s, Kline, OrderIntent, Fill

logger = logging.getLogger(__name__)


class DemoStrategy:
    """
    演示策略

    简单的突破策略：
    - 1s Bar 价格突破最近 N 个 Bar 的高点时做空
    - 持仓后在固定百分比止盈/止损
    """

    def __init__(self, account_id: str, lookback_bars: int = 60):
        """
        Args:
            account_id: 账户ID
            lookback_bars: 回看 Bar 数量
        """
        self.account_id = account_id
        self.lookback_bars = lookback_bars

        # 状态
        self.bar_history: dict[str, list[Bar1s]] = {}
        self.active_positions: set[str] = set()

    def on_bar1s(self, bar: Bar1s) -> list[OrderIntent] | None:
        """
        处理 1s Bar

        Args:
            bar: 1s Bar 数据

        Returns:
            下单意图列表
        """
        symbol = bar.symbol

        # 维护历史 Bar
        if symbol not in self.bar_history:
            self.bar_history[symbol] = []

        self.bar_history[symbol].append(bar)

        # 只保留最近 N 个 Bar
        if len(self.bar_history[symbol]) > self.lookback_bars:
            self.bar_history[symbol].pop(0)

        # 检查是否已有持仓
        if symbol in self.active_positions:
            return None

        # 需要足够的历史数据
        if len(self.bar_history[symbol]) < self.lookback_bars:
            return None

        # 计算最近 N 个 Bar 的最高价
        recent_bars = self.bar_history[symbol][-self.lookback_bars:]
        highest_price = max(b.high for b in recent_bars[:-1])  # 排除当前 Bar

        # 突破策略：当前 Bar 突破最高价
        if bar.high > highest_price:
            # 做空突破
            entry_price = bar.close  # 使用收盘价
            quantity = Decimal('0.001')  # 固定数量

            order_intent = OrderIntent(
                symbol=symbol,
                side='SELL',
                price=entry_price,
                quantity=quantity,
                client_order_id=f"demo_{symbol}_{bar.timestamp}",
                ttl_ms=60000,  # 60秒有效期
                strategy_id='demo_breakout',
                trigger_reason='breakout_high'
            )

            logger.info(
                f"Trigger: {symbol} breakout at {entry_price}, "
                f"highest={highest_price}"
            )

            return [order_intent]

        return None

    def on_kline(self, kline: Kline) -> list[OrderIntent] | None:
        """
        处理 K 线

        Args:
            kline: K 线数据

        Returns:
            下单意图列表（演示策略不使用 K 线）
        """
        return None

    def on_fill(self, fill: Fill) -> None:
        """
        处理成交通知

        Args:
            fill: 成交记录
        """
        symbol = fill.symbol

        if fill.side == 'SELL':
            # 开仓
            self.active_positions.add(symbol)
            logger.info(f"Position opened: {symbol} SHORT @ {fill.price}")

            # TODO: 在实际策略中，这里应该挂止盈止损单

        elif fill.side == 'BUY':
            # 平仓
            if symbol in self.active_positions:
                self.active_positions.remove(symbol)
            logger.info(f"Position closed: {symbol}")


class MinimalStrategy:
    """
    最小化策略示例

    仅实现必需的接口方法，不做任何交易。
    用于测试回测引擎基础功能。
    """

    def __init__(self, account_id: str):
        self.account_id = account_id
        self.bar_count = 0
        self.kline_count = 0

    def on_bar1s(self, bar: Bar1s) -> list[OrderIntent] | None:
        """处理 1s Bar"""
        self.bar_count += 1
        if self.bar_count % 10000 == 0:
            logger.info(f"Processed {self.bar_count} bars")
        return None

    def on_kline(self, kline: Kline) -> list[OrderIntent] | None:
        """处理 K 线"""
        self.kline_count += 1
        return None

    def on_fill(self, fill: Fill) -> None:
        """处理成交通知"""
        pass
