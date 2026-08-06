"""
1s Bar 聚合器
按秒滚动窗口聚合 aggTrade 为 1s Bar
计算 open/high/low/close/volume/vwap
"""
import logging
from collections import defaultdict
from decimal import Decimal
from typing import Any

from trading_platform.shared.events import Bar1s


logger = logging.getLogger(__name__)


class Bar1sAggregator:
    """
    1秒 Bar 聚合器

    按 timestamp // 1000 分桶，每秒滚动窗口聚合 aggTrade
    设置 available_time = timestamp + 1000
    """

    def __init__(self, window_tolerance_ms: int = 5000):
        """
        Args:
            window_tolerance_ms: 允许迟到的时间窗口（毫秒），默认 5 秒
        """
        self.window_tolerance_ms = window_tolerance_ms

        # {symbol: {second_timestamp: TradeWindow}}
        self._windows: dict[str, dict[int, TradeWindow]] = defaultdict(dict)

        # 每个交易对的最新处理时间
        self._last_timestamp: dict[str, int] = {}

        # 已发布窗口不能再修订，避免迟到成交造成同一秒重复 Bar。
        self._finalized_through: dict[str, int] = {}

    def add_trade(
        self,
        symbol: str,
        price: Decimal,
        quantity: Decimal,
        timestamp: int,
    ) -> list[Bar1s]:
        """
        添加一笔交易，返回本次完成的全部 Bar

        Args:
            symbol: 交易对
            price: 价格
            quantity: 数量
            timestamp: 交易时间戳（毫秒）

        Returns:
            按时间升序排列的已完成 Bar1s 列表
        """
        # 计算所属秒级桶
        second_ts = (timestamp // 1000) * 1000

        if second_ts <= self._finalized_through.get(symbol, -1):
            logger.warning(
                f"{symbol} 收到已完成窗口的迟到交易: timestamp={timestamp}, "
                f"finalized_through={self._finalized_through[symbol]}"
            )
            return []

        # 检查是否过于迟到
        last_ts = self._last_timestamp.get(symbol, 0)
        if last_ts > 0 and timestamp < last_ts - self.window_tolerance_ms:
            logger.warning(
                f"{symbol} 收到过期交易: timestamp={timestamp}, "
                f"last_ts={last_ts}, 延迟={last_ts - timestamp}ms"
            )
            return []

        # 获取或创建窗口
        windows = self._windows[symbol]

        if second_ts not in windows:
            windows[second_ts] = TradeWindow(second_ts)

        window = windows[second_ts]
        window.add_trade(price, quantity)

        # 更新最新时间戳
        if timestamp > last_ts:
            self._last_timestamp[symbol] = timestamp

        # 检查是否可以关闭旧窗口
        return self._close_completed_windows(symbol, second_ts)

    def _close_completed_windows(self, symbol: str, current_second: int) -> list[Bar1s]:
        """
        尝试关闭已完成的窗口

        当前窗口如果已经过了 1 秒，且有新的交易进入下一秒，则关闭
        """
        windows = self._windows[symbol]

        # 找出所有可以关闭的窗口（当前时间 - 1 秒以前的）
        last_ts = self._last_timestamp.get(symbol, 0)

        completed_bars = []

        for ts in sorted(windows.keys()):
            # 如果这个窗口已经过去至少 1 秒，且我们收到了更新的数据
            if ts < current_second and last_ts >= ts + 1000:
                window = windows.pop(ts)
                bar = window.to_bar(symbol)
                completed_bars.append(bar)
                self._finalized_through[symbol] = max(
                    ts, self._finalized_through.get(symbol, -1)
                )

        completed_bars.sort(key=lambda bar: bar.timestamp)
        return completed_bars

    def flush_symbol(self, symbol: str) -> list[Bar1s]:
        """
        强制关闭某个交易对的所有窗口（用于清理或关闭订阅时）

        Returns:
            所有完成的 Bar 列表
        """
        windows = self._windows.get(symbol, {})
        bars = []

        for ts in sorted(windows.keys()):
            window = windows[ts]
            bar = window.to_bar(symbol)
            bars.append(bar)

        if symbol in self._windows:
            del self._windows[symbol]
        if symbol in self._last_timestamp:
            del self._last_timestamp[symbol]
        if symbol in self._finalized_through:
            del self._finalized_through[symbol]

        return bars

    def get_stats(self) -> dict[str, Any]:
        """获取聚合器统计信息"""
        return {
            "active_symbols": len(self._windows),
            "total_windows": sum(len(w) for w in self._windows.values()),
            "symbols": {
                symbol: {
                    "windows": len(windows),
                    "last_timestamp": self._last_timestamp.get(symbol, 0),
                }
                for symbol, windows in self._windows.items()
            },
        }


class TradeWindow:
    """
    单秒交易窗口
    """

    def __init__(self, timestamp: int):
        self.timestamp = timestamp  # 秒开始时间
        self.open: Decimal | None = None
        self.high: Decimal | None = None
        self.low: Decimal | None = None
        self.close: Decimal | None = None
        self.volume = Decimal("0")
        self.trade_count = 0
        self.quote_volume = Decimal("0")  # 用于计算 vwap

    def add_trade(self, price: Decimal, quantity: Decimal) -> None:
        """添加一笔交易到窗口"""
        if self.open is None:
            self.open = price

        if self.high is None or price > self.high:
            self.high = price

        if self.low is None or price < self.low:
            self.low = price

        self.close = price
        self.volume += quantity
        self.quote_volume += price * quantity
        self.trade_count += 1

    def to_bar(self, symbol: str) -> Bar1s:
        """转换为 Bar1s"""
        # 计算 vwap
        vwap = self.quote_volume / self.volume if self.volume > 0 else self.close or Decimal("0")

        return Bar1s(
            symbol=symbol,
            timestamp=self.timestamp,
            available_time=self.timestamp + 1000,
            open=self.open or Decimal("0"),
            high=self.high or Decimal("0"),
            low=self.low or Decimal("0"),
            close=self.close or Decimal("0"),
            volume=self.volume,
            trade_count=self.trade_count,
            vwap=vwap,
            type_priority=1,
            sequence=0,
        )
