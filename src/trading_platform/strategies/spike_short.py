"""
Dynamic Spike Short Strategy - 迁移到新平台

策略逻辑:
1. 检测逼空信号：5秒涨幅>5%，5秒成交量>中位数3倍
2. 验证起涨点：origin价格 + 12小时低点验证
3. 三档分层做空：使用ATR计算回撤目标价
4. 无效价格保护：spike_high + 3.5*ATR触及则失效

迁移自: scripts/backtest_dynamic_spike.py
"""
from decimal import Decimal
from datetime import datetime, timedelta
from typing import List, Optional
from dataclasses import dataclass

from trading_platform.shared.events import Bar1s, Kline, OrderIntent
from trading_platform.backtest.engine import BacktestEngine


@dataclass
class SpikeSignal:
    """逼空信号"""
    signal_time: int  # 毫秒时间戳
    trigger_price: Decimal
    spike_high: Decimal
    origin_price: Decimal
    atr: Decimal
    tier_prices: List[Decimal]  # 3档做空价格
    tier_weights: List[Decimal]  # 3档仓位权重
    invalid_price: Decimal  # 无效价格
    active_time: int  # 订单激活时间
    expire_time: int  # 订单过期时间


class DynamicSpikeShortStrategy:
    """
    动态逼空做空策略

    策略特点:
    - 检测突然的价格飙升和成交量激增
    - 三档分层做空，降低风险
    - ATR动态计算目标价
    - 无效价格保护机制
    """

    # 策略参数
    TIER_WEIGHTS = (Decimal("0.30"), Decimal("0.40"), Decimal("0.30"))
    RETEST_ATR = Decimal("0.75")  # 主目标位ATR倍数
    SPREAD_ATR = Decimal("0.40")  # 档位间隔ATR倍数
    ORIGIN_MIN_RISE = Decimal("0.10")  # origin价格最小上涨10%

    SPIKE_RISE_5S = Decimal("0.05")  # 5秒涨幅阈值
    VOLUME_MULTIPLE_5S = Decimal("3.0")  # 5秒成交量倍数
    RISE_FROM_12H_LOW = Decimal("0.20")  # 12小时低点涨幅阈值
    SIGNAL_COOLDOWN = 180  # 信号冷却时间（秒）
    ORDER_TTL = 180  # 订单有效期（秒）

    def __init__(self, symbol: str, account_id: str = "backtest"):
        self.symbol = symbol
        self.account_id = account_id

        # 数据缓存
        self.bars_1s: List[Bar1s] = []  # 最近60秒的1s Bar
        self.klines_1m: List[Kline] = []  # 最近30小时的1分钟K线
        self.klines_5m: List[Kline] = []  # 最近40小时的5分钟K线

        # 信号状态
        self.last_signal_time: Optional[int] = None
        self.active_signals: List[SpikeSignal] = []

        # 持仓状态
        self.positions: dict = {}  # symbol -> quantity

    def on_bar1s(self, bar: Bar1s, engine: BacktestEngine) -> List[OrderIntent]:
        """处理1秒Bar事件"""
        # 更新缓存
        self._update_cache(bar)

        # 需要至少60秒数据
        if len(self.bars_1s) < 60:
            return []

        # 检测信号
        signal = self._detect_signal(bar, engine.virtual_clock)
        if signal:
            self.active_signals.append(signal)
            self.last_signal_time = signal.signal_time

        # 生成订单意图
        intents = []

        # 检查活跃信号的订单
        for sig in self.active_signals[:]:
            if bar.timestamp >= sig.expire_time:
                # 订单过期，移除信号
                self.active_signals.remove(sig)
                continue

            if bar.timestamp < sig.active_time:
                # 还未到激活时间
                continue

            # 检查无效价格
            if bar.high >= sig.invalid_price:
                # 触及无效价格，取消所有挂单
                self.active_signals.remove(sig)
                continue

            # 检查三档挂单
            for tier_idx, (tier_price, tier_weight) in enumerate(
                zip(sig.tier_prices, sig.tier_weights), start=1
            ):
                order_id = f"spike_short_tier{tier_idx}_{sig.signal_time}"

                # 检查是否已下单
                existing_order = engine.executor.orders.get(order_id)
                if existing_order:
                    continue

                # 检查价格是否满足origin_floor
                origin_floor = sig.origin_price * (Decimal("1") + self.ORIGIN_MIN_RISE)
                if tier_price < origin_floor or tier_price <= sig.trigger_price:
                    continue

                # 计算仓位（假设账户权益10000 USDT）
                account_equity = Decimal("10000")
                position_value = account_equity * tier_weight
                quantity = position_value / tier_price

                # 创建做空挂单
                intent = OrderIntent(
                    symbol=self.symbol,
                    side="SELL",
                    price=tier_price,
                    quantity=quantity,
                    client_order_id=order_id,
                    ttl_ms=int((sig.expire_time - bar.timestamp)),
                )
                intents.append(intent)

        return intents

    def on_kline(self, kline: Kline) -> List[OrderIntent]:
        """处理K线事件"""
        # 更新K线缓存
        if kline.interval == "1m":
            self.klines_1m.append(kline)
            # 只保留最近30小时
            cutoff = kline.close_time - 30 * 3600 * 1000
            self.klines_1m = [k for k in self.klines_1m if k.close_time >= cutoff]

        elif kline.interval == "5m":
            self.klines_5m.append(kline)
            # 只保留最近40小时
            cutoff = kline.close_time - 40 * 3600 * 1000
            self.klines_5m = [k for k in self.klines_5m if k.close_time >= cutoff]

        return []

    def _update_cache(self, bar: Bar1s):
        """更新1秒Bar缓存"""
        self.bars_1s.append(bar)
        # 只保留最近60秒
        if len(self.bars_1s) > 60:
            self.bars_1s = self.bars_1s[-60:]

    def _detect_signal(self, current_bar: Bar1s, current_time: int) -> Optional[SpikeSignal]:
        """检测逼空信号"""
        # 1. 检查信号冷却
        if self.last_signal_time:
            if (current_time - self.last_signal_time) < self.SIGNAL_COOLDOWN * 1000:
                return None

        # 2. 检查5秒涨幅和成交量
        if len(self.bars_1s) < 60:
            return None

        # 最近5秒的Bar
        bars_5s = self.bars_1s[-5:]
        if not bars_5s or len(bars_5s) < 5:
            return None

        # 5秒涨幅
        rise_5s = current_bar.close / bars_5s[0].open - Decimal("1")
        if rise_5s < self.SPIKE_RISE_5S:
            return None

        # 5秒总成交量
        volume_5s = sum(b.volume for b in bars_5s)

        # 过去60秒成交量中位数
        volumes_60s = sorted([b.volume for b in self.bars_1s])
        median_volume = volumes_60s[30]

        # 成交量倍数
        volume_multiple = volume_5s / (median_volume * Decimal("5"))
        if volume_multiple < self.VOLUME_MULTIPLE_5S:
            return None

        # 3. 检查12小时低点涨幅
        if not self.klines_1m:
            return None

        # 获取12小时内的最低价
        lookback_12h = current_time - 12 * 3600 * 1000
        recent_1m = [k for k in self.klines_1m if k.open_time >= lookback_12h]
        if not recent_1m:
            return None

        low_12h = min(k.low for k in recent_1m)
        rise_from_12h = current_bar.close / low_12h - Decimal("1")
        if rise_from_12h < self.RISE_FROM_12H_LOW:
            return None

        # 4. 计算origin价格（16小时最低价）
        lookback_16h = current_time - 16 * 3600 * 1000
        origin_klines = [k for k in self.klines_1m if k.open_time >= lookback_16h]
        if not origin_klines:
            return None
        origin_price = min(k.low for k in origin_klines)

        # 5. 计算spike_high（最近30分钟最高价）
        lookback_30m = current_time - 30 * 60 * 1000
        recent_30m = [k for k in self.klines_1m if k.open_time >= lookback_30m]
        if not recent_30m:
            return None
        spike_high = max([current_bar.high] + [k.high for k in recent_30m])

        # 6. 计算ATR（5分钟，14周期）
        if len(self.klines_5m) < 15:
            return None

        true_ranges = []
        for i in range(1, min(15, len(self.klines_5m))):
            k = self.klines_5m[-i]
            k_prev = self.klines_5m[-i-1]
            tr = max(
                k.high - k.low,
                abs(k.high - k_prev.close),
                abs(k.low - k_prev.close)
            )
            true_ranges.append(tr)

        if not true_ranges:
            return None
        atr = sum(true_ranges) / len(true_ranges)

        # 7. 计算三档价格
        tier_prices = [
            spike_high - atr * (self.RETEST_ATR - Decimal(n-1) * self.SPREAD_ATR)
            for n in range(1, 4)
        ]

        # 8. 验证价格合理性
        origin_floor = origin_price * (Decimal("1") + self.ORIGIN_MIN_RISE)
        if min(tier_prices) < origin_floor or min(tier_prices) <= current_bar.close:
            return None

        # 9. 计算无效价格
        invalid_price = max(
            spike_high + atr * Decimal("3.5"),
            tier_prices[1] + atr * Decimal("2.0")
        )

        # 10. 创建信号
        signal = SpikeSignal(
            signal_time=current_time,
            trigger_price=current_bar.close,
            spike_high=spike_high,
            origin_price=origin_price,
            atr=atr,
            tier_prices=tier_prices,
            tier_weights=list(self.TIER_WEIGHTS),
            invalid_price=invalid_price,
            active_time=current_time + 1000,  # 下一秒激活
            expire_time=current_time + self.ORDER_TTL * 1000,
        )

        return signal


# 回测适配器
class DynamicSpikeBacktestStrategy:
    """适配回测引擎的策略包装器"""

    def __init__(self, symbols: List[str]):
        self.strategies = {
            symbol: DynamicSpikeShortStrategy(symbol)
            for symbol in symbols
        }

    def on_event(self, event, engine: BacktestEngine) -> List[OrderIntent]:
        """事件处理入口"""
        if isinstance(event, Bar1s):
            strategy = self.strategies.get(event.symbol)
            if strategy:
                return strategy.on_bar1s(event, engine)

        elif isinstance(event, Kline):
            strategy = self.strategies.get(event.symbol)
            if strategy:
                return strategy.on_kline(event)

        return []
