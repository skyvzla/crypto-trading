"""
研究专用 Spike 策略：真实触发判定 + 触发时点特征 + 三高/atr 过滤 + 动态溢价单档挂单
+ 15m>8% 止损 / 4h 到期平仓。仅用于研究回测，不可用于 testnet/live。

与实盘口径对齐：
- 触发判定完全复用 DynamicSpikeShortStrategy._detect_signal（1s 粒度、rise_5s、量能、
  连续性、12h 低点、冷却）
- 特征全部使用触发时点可得信息（已完成 1m/5m bar + 当前未完成分钟 1s 实时累计），
  不包含任何事后（osc_end 后）信息
- 挂单成交/手续费/撤单走回测引擎统一成交模型
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import List, Optional

import numpy as np

from trading_platform.research.indicators import (
    atr,
    ema,
    macd,
    obv_slope,
    rsi,
    stochastic,
)
from trading_platform.shared.events import Bar1s, Fill, OrderIntent
from trading_platform.shared.execution import StrategyAccount
from trading_platform.strategies.spike.short import (
    DynamicSpikeBacktestStrategy,
    DynamicSpikeShortStrategy,
    MS_PER_MINUTE,
    MS_PER_SECOND,
    SpikeSignal,
)

RESEARCH_ORDER_TTL_SECONDS = 7200  # 挂单有效期 2h（研究口径）
RESEARCH_HOLD_MS = 4 * 3600 * MS_PER_SECOND  # 4h 到期平仓
RESEARCH_STOP_CHECK_MS = 15 * 60 * MS_PER_SECOND  # 15m 止损检查时点
KLINE_1M_RETAIN_MINUTES = 1100  # 16h origin 窗口(960) + 12h 低点(720) + 特征
MAX_1S_FILL_SECONDS = 300  # 归档去空秒；实盘 1s 线连续，回测补秒上限

FEATURES = [
    "vwap_dev_5m",
    "ema_ratio_5m",
    "roc_5m",
    "amplitude_pct",
    "accel_5m",
    "pulse_1m",
    "consecutive_green",
    "vol_cv_1h",
    "rsi_5m",
    "sto_k_5m",
    "green_share_1m",
    "macd_hist_5m",
    "obv_slope_5m",
]

MODEL_FEATURES = FEATURES
TRIPLE_HIGH_FEATURES = ["vwap_dev_5m", "ema_ratio_5m", "roc_5m"]


@dataclass
class ResearchParams:
    """研究参数（与 1m 口径研究结论一一对应）。"""

    triple_high_thresholds: dict[str, float] = field(default_factory=dict)
    atr_min: float = 0.0
    premium_mult: float = 0.7
    premium_floor_pct: float = 3.0
    premium_cap_pct: float = 35.0
    stop_loss_pct: float = 8.0
    stop_check_after_ms: int = RESEARCH_STOP_CHECK_MS  # 0=入场后立即逐bar监控(绝对止损)
    hold_ms: int = RESEARCH_HOLD_MS
    model_mean: dict[str, float] | None = None
    model_std: dict[str, float] | None = None
    model_coefs: dict[str, float] | None = None
    model_intercept: float = 0.0


def compute_trigger_features(
    strategy: DynamicSpikeShortStrategy, bar: Bar1s, origin_price: Decimal
) -> dict[str, float] | None:
    """触发时点可得特征。数据不足返回 None。

    所有窗口特征统一口径：已完成 bar 序列 + 当前未完成分钟 1s 实时累计收尾，
    与实盘 on_bar1s 时刻可计算的数据完全一致。
    """
    minute_start = bar.timestamp - (bar.timestamp % MS_PER_MINUTE)
    c = float(bar.close)
    if origin_price is None or origin_price <= 0:
        return None

    k5 = list(strategy.klines_5m)
    if len(k5) < 26:
        return None
    c5_raw = np.array([float(k.close) for k in k5], dtype=float)
    h5_raw = np.array([float(k.high) for k in k5], dtype=float)
    l5_raw = np.array([float(k.low) for k in k5], dtype=float)
    v5_raw = np.array([float(k.volume) for k in k5], dtype=float)

    # 未完成 5m：minute_start 之后的 1s bars 实时累计
    cur_secs = [b for b in strategy.bars_1s if b.timestamp >= minute_start]
    cur_high = max(float(b.high) for b in cur_secs) if cur_secs else c
    cur_low = min(float(b.low) for b in cur_secs) if cur_secs else c
    cur_vol = sum(float(b.volume) for b in cur_secs) if cur_secs else 0.0

    c5 = np.concatenate([c5_raw, [c]])
    h5 = np.concatenate([h5_raw, [cur_high]])
    l5 = np.concatenate([l5_raw, [cur_low]])
    v5 = np.concatenate([v5_raw, [cur_vol]])
    n5 = len(c5)

    # 1m 序列（已完成 + 未完成分钟实时 close/vol）
    k1 = list(strategy.klines_1m)
    if len(k1) < 60:
        return None
    c1 = np.array([float(k.close) for k in k1], dtype=float)
    v1 = np.array([float(k.volume) for k in k1], dtype=float)
    c1_series = np.concatenate([c1, [c]])
    v1_series = np.concatenate([v1, [cur_vol]])

    out: dict[str, float] = {}

    # vwap_dev_5m：前 100m 聚合 20 根 5m VWAP 偏离（策略已收盘 1m 口径）
    vwap = strategy._vwap_deviation_filter(minute_start, bar.close)
    if vwap is None:
        return None
    out["vwap_dev_5m"] = vwap["spike_vwap_deviation_pct"]

    e20 = ema(c5, 20)[-1]
    out["ema_ratio_5m"] = float(c / e20) if e20 > 0 else float("nan")
    out["rsi_5m"] = float(rsi(c5, 14)[-1])
    _, _, mh = macd(c5, 12, 26, 9)
    out["macd_hist_5m"] = float(mh[-1])
    out["sto_k_5m"] = float(stochastic(c5, 14)[0])
    a5 = atr(h5, l5, c5, 14)[-1]
    out["atr_ratio_5m"] = float(a5 / c) if c > 0 else float("nan")
    out["obv_slope_5m"] = float(obv_slope(c5, v5, 20))

    if n5 >= 11:
        ret5 = c / c5[-6] - 1.0
        ret_prev5 = c5[-6] / c5[-10] - 1.0 if n5 >= 10 else 0.0
        out["roc_5m"] = ret5 * 100.0
        out["accel_5m"] = ret5 - ret_prev5
    else:
        return None

    if len(c1_series) >= 31:
        d = np.diff(c1_series[-31:])
        prev = c1_series[-31:-1]
        with np.errstate(divide="ignore", invalid="ignore"):
            pulse = np.max(d / prev) if np.all(prev > 0) else np.nan
        out["pulse_1m"] = float(pulse) if np.isfinite(pulse) else float("nan")
    else:
        return None

    n = 0
    for i in range(len(c1) - 1, 0, -1):
        if c1[i] > c1[i - 1]:
            n += 1
        else:
            break
    if c > c1[-1]:
        n += 1
    out["consecutive_green"] = float(n)

    if len(v1_series) >= 60:
        seg = v1_series[-60:]
        mu = float(np.mean(seg))
        out["vol_cv_1h"] = float(np.std(seg) / mu) if mu > 0 else float("nan")
        up = int(
            sum(
                1
                for i in range(len(c1) - 60, len(c1))
                if c1[i] > c1[i - 1]
            )
        )
        out["green_share_1m"] = up / 60.0
    else:
        return None

    out["amplitude_pct"] = (c / float(origin_price) - 1.0) * 100.0
    return out


def _predict_premium(feats: dict[str, float], params: ResearchParams) -> float | None:
    """13 特征线性模型预测冲高%，返回动态溢价%（已 clip）。"""
    if params.model_mean is None or params.model_std is None or params.model_coefs is None:
        return None
    total = float(params.model_intercept)
    for name in MODEL_FEATURES:
        if name not in feats or not np.isfinite(feats[name]):
            return None
        mu = params.model_mean.get(name)
        sd = params.model_std.get(name)
        w = params.model_coefs.get(name)
        if mu is None or not sd or w is None:
            return None
        total += w * (feats[name] - mu) / sd
    return float(
        np.clip(total * params.premium_mult, params.premium_floor_pct, params.premium_cap_pct)
    )


def _gapfill_1s(strategy: DynamicSpikeShortStrategy, bar: Bar1s) -> None:
    """归档 1s 去空秒（无成交秒不落盘），实盘 1s 线连续；
    按前值补缺失秒（量 0），保证 rise_5s/连续性检查与实盘一致。"""
    if not strategy.bars_1s:
        return
    prev = strategy.bars_1s[-1]
    gap_seconds = (bar.timestamp - prev.timestamp) // MS_PER_SECOND - 1
    if gap_seconds <= 0 or gap_seconds > MAX_1S_FILL_SECONDS:
        return
    for k in range(1, gap_seconds + 1):
        t = prev.timestamp + k * MS_PER_SECOND
        strategy._update_cache(
            Bar1s(
                symbol=strategy.symbol,
                timestamp=t,
                open=prev.close,
                high=prev.close,
                low=prev.close,
                close=prev.close,
                volume=Decimal("0"),
                trade_count=0,
                vwap=prev.close,
                available_time=t + MS_PER_SECOND,
            )
        )


def _filter_pass(feats: dict[str, float], params: ResearchParams) -> bool:
    for name in TRIPLE_HIGH_FEATURES:
        th = params.triple_high_thresholds.get(name)
        if th is not None and not (feats.get(name, -1e9) > th):
            return False
    if params.atr_min > 0 and not (feats.get("atr_ratio_5m", -1e9) > params.atr_min):
        return False
    return True


class ResearchPremiumShortStrategy(DynamicSpikeShortStrategy):
    """真实触发判定 + 研究过滤/挂单/退出。"""

    def __init__(
        self,
        symbol: str,
        total_notional: Decimal,
        account: Optional[StrategyAccount] = None,
        account_id: str = "backtest",
        research_params: Optional[ResearchParams] = None,
        **kwargs,
    ):
        # 研究口径禁用前高过滤（事件集合对齐研究）；其余触发判定保持真实。
        kwargs["prior_high_lookback_minutes"] = 1
        kwargs["exit_policy"] = "execution-test-d007"
        super().__init__(symbol, total_notional, account=account, account_id=account_id, **kwargs)
        self.prior_high_lookback_minutes = 0
        self.research_params = research_params or ResearchParams()
        self.trade_records: list[dict] = []
        self._peak: dict[str, float] = {}
        self._entry_price: dict[str, float] = {}

    def _prior_high_point(self, minute_start: int):
        """研究口径禁用前高过滤（0 会导致窗口为空返回 None 拦掉信号）。"""
        return Decimal("0"), minute_start

    # -- 1m 缓存延长（特征需要 60 根已完成 1m） -------------------------------
    def on_kline(self, kline) -> List[OrderIntent]:
        if self._shared_feature_provider is None and kline.interval == "1m":
            cutoff = kline.close_time - KLINE_1M_RETAIN_MINUTES * MS_PER_MINUTE
            self._append_kline_and_evict_expired("1m", self.klines_1m, kline, cutoff)
            return []
        return super().on_kline(kline)

    # -- 触发判定 + 研究过滤 + 动态溢价挂单 -----------------------------------
    def on_bar1s(self, bar: Bar1s) -> List[OrderIntent]:
        _gapfill_1s(self, bar)
        return super().on_bar1s(bar)

    def _detect_signal(self, bar: Bar1s) -> Optional[SpikeSignal]:
        signal = super()._detect_signal(bar)
        if signal is None:
            return None
        feats = compute_trigger_features(self, bar, signal.origin_price)
        if feats is None:
            return None
        if not _filter_pass(feats, self.research_params):
            return None
        premium = _predict_premium(feats, self.research_params)
        if premium is None:
            return None
        limit = signal.trigger_price * (Decimal("1") + Decimal(str(premium / 100.0)))
        atr_ratio = feats.get("atr_ratio_5m") or 0.04
        signal.tier_prices = [limit, limit, limit]
        signal.tier_weights = [Decimal("1"), Decimal("0"), Decimal("0")]
        signal.invalid_price = max(
            signal.invalid_price,
            limit * (Decimal("1") + Decimal(str(2.0 * atr_ratio))),
        )
        signal.expire_time = signal.active_time + RESEARCH_ORDER_TTL_SECONDS * MS_PER_SECOND
        signal.research_premium = premium
        signal.research_feats = feats
        return signal

    # -- 退出：15m>8% 止损 + 4h 到期 ------------------------------------------
    def _manage_non_positive_timeout(self, bar: Bar1s) -> List[OrderIntent]:
        if self._exit_requested or self._account is None or self.first_fill_time is None:
            return []
        position = self._account.get_position(self.symbol)
        if position is None or position.side != "SHORT" or position.quantity <= 0:
            return []

        elapsed = bar.available_time - self.first_fill_time
        key = self._campaign_id_for_timing
        entry = float(position.entry_price)
        if key is not None:
            entry = self._entry_price.get(key) or entry
            self._entry_price[key] = entry
            self._peak[key] = max(self._peak.get(key, float(bar.close)), float(bar.high))
        mark = float(bar.close)
        pnl_pct = (entry - mark) / entry * 100.0

        reason = None
        if elapsed >= self.research_params.hold_ms:
            reason = "research_hold_exit"
        elif (
            elapsed >= self.research_params.stop_check_after_ms
            and pnl_pct < -self.research_params.stop_loss_pct
        ):
            reason = "research_stop_exit"

        if reason is None:
            return []

        mae_pct = (self._peak.get(key, mark) / entry - 1.0) * 100.0 if key is not None else 0.0
        self.trade_records.append(
            {
                "symbol": self.symbol,
                "campaign_id": key,
                "entry_price": round(entry, 8),
                "exit_price": round(mark, 8),
                "pnl_pct": round(pnl_pct, 4),
                "mae_pct": round(mae_pct, 4),
                "reason": reason,
                "hold_ms": elapsed,
            }
        )
        self._exit_requested = True
        self._record_audit(
            event_time=bar.available_time,
            event_type=reason + "_requested",
            campaign_id=self._campaign_id_for_timing,
            details={
                "entry_price": str(position.entry_price),
                "observed_close": str(bar.close),
                "pnl_pct": str(round(pnl_pct, 4)),
                "quantity": str(position.quantity),
            },
        )
        return [
            OrderIntent(
                symbol=self.symbol,
                side="BUY",
                price=bar.close,
                quantity=position.quantity,
                client_order_id=f"{key or 'spike_short'}_{reason}",
                order_type="MARKET",
                reduce_only=True,
                strategy_id="spike_short",
                trigger_reason=reason,
            )
        ]

    def on_fill(self, fill: Fill) -> None:
        super().on_fill(fill)
        if fill.side == "SELL" and fill.symbol == self.symbol:
            key = self._campaign_id_for_timing
            if key is not None:
                self._entry_price[key] = float(fill.price)
                self._peak[key] = max(self._peak.get(key, 0.0), float(fill.price))

    def drain_trade_records(self) -> List[dict]:
        records = self.trade_records
        self.trade_records = []
        return records

    def reset_campaign_timing(self) -> None:
        super().reset_campaign_timing()
        key = getattr(self, "_campaign_id_for_timing", None)
        if key is not None:
            self._peak.pop(key, None)
            self._entry_price.pop(key, None)


class ResearchPremiumBacktestStrategy(DynamicSpikeBacktestStrategy):
    """多币种研究适配器。"""

    def __init__(
        self,
        symbols: List[str],
        total_notional: Decimal,
        account: Optional[StrategyAccount] = None,
        research_params: Optional[ResearchParams] = None,
    ):
        super().__init__(
            symbols,
            total_notional,
            account=account,
            strategy_class=ResearchPremiumShortStrategy,
            strategy_parameters={"research_params": research_params or ResearchParams()},
        )

    def drain_trade_records(self) -> List[dict]:
        records: List[dict] = []
        for strategy in self.strategies.values():
            records.extend(strategy.drain_trade_records())
        return records


class RecordOnlyShortStrategy(DynamicSpikeShortStrategy):
    """记录模式：真实触发判定 + 触发时点特征 + 触发后 4h 冲高/回落，
    不挂单不平仓，用于生成训练集与阈值统计。"""

    def __init__(
        self,
        symbol: str,
        total_notional: Decimal,
        account: Optional[StrategyAccount] = None,
        account_id: str = "backtest",
        **kwargs,
    ):
        kwargs["prior_high_lookback_minutes"] = 1
        kwargs["exit_policy"] = "execution-test-d007"
        super().__init__(symbol, total_notional, account=account, account_id=account_id, **kwargs)
        self.prior_high_lookback_minutes = 0
        self.records: list[dict] = []
        self._open: list[dict] = []

    def _prior_high_point(self, minute_start: int):
        """研究口径禁用前高过滤。"""
        return Decimal("0"), minute_start

    def on_kline(self, kline) -> List[OrderIntent]:
        if self._shared_feature_provider is None and kline.interval == "1m":
            cutoff = kline.close_time - KLINE_1M_RETAIN_MINUTES * MS_PER_MINUTE
            self._append_kline_and_evict_expired("1m", self.klines_1m, kline, cutoff)
            return []
        return super().on_kline(kline)

    def on_bar1s(self, bar: Bar1s) -> List[OrderIntent]:
        _gapfill_1s(self, bar)
        self._update_cache(bar)
        self._settle_open(bar)
        if len(self.bars_1s) < self.BAR_BUFFER:
            return []
        if self.last_signal_time is not None:
            if bar.timestamp - self.last_signal_time < self.SIGNAL_COOLDOWN * MS_PER_SECOND:
                return []
        signal = self._detect_signal(bar)
        if signal is None:
            return []
        self.last_signal_time = signal.signal_time
        feats = compute_trigger_features(self, bar, signal.origin_price)
        if feats is None:
            return []
        self._open.append(
            {
                "symbol": self.symbol,
                "trig_t": signal.signal_time,
                "trig_price": float(signal.trigger_price),
                "peak": float(signal.trigger_price),
                "close_4h": None,
                **feats,
            }
        )
        return []

    def _settle_open(self, bar: Bar1s) -> None:
        for rec in list(self._open):
            if bar.high > rec["peak"]:
                rec["peak"] = float(bar.high)
            if bar.timestamp >= rec["trig_t"] + RESEARCH_HOLD_MS:
                rec["close_4h"] = float(bar.close)
                rec["total_up_pct"] = (rec["peak"] / rec["trig_price"] - 1) * 100.0
                rec["pnl_4h_pct"] = (
                    (rec["trig_price"] - float(bar.close)) / rec["trig_price"] * 100.0
                )
                self.records.append(rec)
                self._open.remove(rec)

    def drain_records(self) -> List[dict]:
        records = self.records
        self.records = []
        return records


class RecordOnlyBacktestStrategy(DynamicSpikeBacktestStrategy):
    """多币种记录模式适配器。"""

    def __init__(
        self,
        symbols: List[str],
        total_notional: Decimal,
        account: Optional[StrategyAccount] = None,
    ):
        super().__init__(
            symbols,
            total_notional,
            account=account,
            strategy_class=RecordOnlyShortStrategy,
        )

    def drain_records(self) -> List[dict]:
        records: List[dict] = []
        for strategy in self.strategies.values():
            records.extend(strategy.drain_records())
        return records