"""
Spike v3：大插针回落接空做空。

与 v1/v2 的区别（独立实现，不继承 DynamicSpikeShortStrategy）：
- 入场不预测冲高、不挂三档 ATR 限价单；改为等价格回吐插针涨幅后回落接空。
  信号 = 移动 60s 涨幅达到阈值 + 6h 起涨背景
        + 插针总涨幅达到阈值 + 价格回吐 retrace_frac 涨幅
        + 4h 前高过滤 + 买卖比轻过滤（可选）。
  入场以市价单模拟"回落触及成交"，成交价=max(candidate, bar.open)。
- 退出与 v1 一致（candidate-v1 状态机：动量衰减/时间风险/浮盈回撤/通道突破），
  额外增加 5m 插针高点止损（持仓>5min 重新触及插针高点即平）、可选硬止盈、1h 超时兜底。
- 入场后 spike_high 冻结在入场时刻，用于 5m 插针高点止损。

研究对象: tools/research_pullback_short.py
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from decimal import Decimal
from typing import List, Optional

from trading_platform.shared.events import (
    Bar1s,
    Fill,
    Kline,
    OrderIntent,
    StrategyAuditEvent,
)
from trading_platform.shared.execution import StrategyAccount
from trading_platform.strategies.spike.definition import (
    SPIKE_CANDIDATE_EXIT_FEATURE,
    SPIKE_MIN_LOW_1M_FEATURE,
    SPIKE_PRIOR_HIGH_1M_FEATURE,
    SPIKE_RISE_60S_FEATURE,
    SPIKE_V2_SHARED_METRICS,
    SpikeDataRequirements,
    SpikeStrategyDefaults,
)
from trading_platform.strategies.spike.exit_features import (
    CandidateFeatureConfig,
    CandidateFeatureSnapshot,
    candidate_feature_snapshot,
)
from trading_platform.strategies.spike.exit_policy import (
    CandidateV1Config,
    ExitAction,
    ExitObservation,
    SpikeExitPolicyState,
    candidate_v1_risks,
)
from trading_platform.strategies.spike.shared_features import (
    SpikeSharedFeatureProvider,
    append_kline_and_evict_expired,
)
from trading_platform.strategies.spike.short import build_exit_client_order_id

MS_PER_SECOND = 1000
MS_PER_MINUTE = 60 * MS_PER_SECOND
MS_PER_HOUR = 3600 * MS_PER_SECOND

# 入场检测需要的 1s Bar 数量：索引 i-60 .. i
BAR_BUFFER = 61


def _base36(value: int) -> str:
    if value < 0:
        raise ValueError("base36 value must be non-negative")
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    if value == 0:
        return "0"
    encoded = ""
    while value:
        value, remainder = divmod(value, 36)
        encoded = alphabet[remainder] + encoded
    return encoded


def _build_entry_client_order_id(symbol: str, signal_time: int) -> str:
    value = f"p_{symbol}_{_base36(signal_time)}"
    return value


def _campaign_id(symbol: str, signal_time: int) -> str:
    return f"pullback_v3:{symbol}:{signal_time}"


@dataclass
class _PendingEntry:
    """已检测到 60s 暴涨、等待大插针确认与回落接空的待入场状态。"""

    signal_ms: int
    origin_price: Decimal
    spike_high: Decimal
    prior_high: Decimal | None = None
    prior_high_time: int | None = None
    retrace_reached: bool = False
    prior_high_blocked: bool = False


class PullbackV3Strategy:
    """Spike v3 单币种策略核心（回落接空）。"""

    strategy_name = "pullback-v3"

    def __init__(
        self,
        symbol: str,
        total_notional: Decimal,
        account: Optional[StrategyAccount] = None,
        *,
        # 入场
        rise_60s_threshold: Decimal = Decimal("0.40"),
        rise_window_seconds: int = 60,
        cooldown_seconds: int = 180,
        min_spike_rise: Decimal = Decimal("0.40"),
        retrace_frac: Decimal = Decimal("0.30"),
        rise_low_lookback_hours: int = 24,
        min_rise_duration_hours: int = 6,
        prior_high_lookback_hours: int = 4,
        prior_high_tolerance_percent: Decimal = Decimal("0"),
        buy_ratio_entry_min: Decimal = Decimal("0"),
        # 退出（candidate-v1 一致）
        exit_strict_age_ms: int | None = None,
        exit_flat_agreement: int | None = None,
        time_risk_grace_ms: int = 0,
        time_risk_grace_loss_ratio: Decimal = Decimal("0.01"),
        strong_strict_age_ms: int | None = None,
        weak_strict_age_ms: int | None = None,
        strong_bucket_strict_age_ms: int | None = None,
        weak_bucket_strict_age_ms: int | None = None,
        profit_unlock_ratio: Decimal | None = None,
        profit_drawdown_ratio: Decimal | None = None,
        profit_drawdown_peak_ratio: Decimal | None = None,
        early_profit_unlock_ratio: Decimal | None = None,
        exit_stable_breakout_age_ms: int = 0,
        stable_breakout_gate_stop_pct: Decimal | None = None,
        # 额外退出
        stop_5m_high: bool = True,
        take_profit: Decimal = Decimal("0"),
        max_hold_seconds: int = 3600,
        wait_seconds: int = 90,
    ):
        if total_notional is None or total_notional <= 0:
            raise ValueError("total_notional must be a positive Decimal")
        rise_low_lookback_hours = int(rise_low_lookback_hours)
        min_rise_duration_hours = int(min_rise_duration_hours)
        prior_high_lookback_hours = int(prior_high_lookback_hours)
        if min_rise_duration_hours > rise_low_lookback_hours:
            raise ValueError(
                "min_rise_duration_hours must not exceed rise_low_lookback_hours"
            )
        rise_60s_threshold = Decimal(str(rise_60s_threshold))
        if rise_60s_threshold < 0:
            raise ValueError("rise_60s_threshold must not be negative")
        if prior_high_lookback_hours < 0:
            raise ValueError("prior_high_lookback_hours must not be negative")
        prior_high_tolerance_percent = Decimal(str(prior_high_tolerance_percent))
        if not Decimal("0") <= prior_high_tolerance_percent <= Decimal("100"):
            raise ValueError("prior_high_tolerance_percent must be between 0 and 100")
        if cooldown_seconds < 0:
            raise ValueError("cooldown_seconds must not be negative")
        if wait_seconds < 0:
            raise ValueError("wait_seconds must not be negative")
        if exit_stable_breakout_age_ms is not None and exit_stable_breakout_age_ms < 0:
            raise ValueError("exit_stable_breakout_age_ms must not be negative")
        self.exit_stable_breakout_age_ms = int(exit_stable_breakout_age_ms or 0)
        if (
            rise_window_seconds is not None
            and not 60 <= rise_window_seconds <= 3600
        ):
            raise ValueError("rise_window_seconds must be between 60 and 3600")
        self.rise_window_seconds = int(rise_window_seconds or 60)
        self.rise_window_bars = self.rise_window_seconds + 1
        self.stable_breakout_gate_stop_pct = (
            Decimal(str(stable_breakout_gate_stop_pct))
            if stable_breakout_gate_stop_pct is not None
            else None
        )
        # 0 哨兵=关闭（沿用 TOML 0 表示 None 的项目惯例）
        if self.stable_breakout_gate_stop_pct == 0:
            self.stable_breakout_gate_stop_pct = None
        if (
            self.stable_breakout_gate_stop_pct is not None
            and not Decimal("0") < self.stable_breakout_gate_stop_pct <= Decimal("1")
        ):
            raise ValueError("stable_breakout_gate_stop_pct must be in (0, 1] or 0 to disable")
        self.symbol = symbol
        self.total_notional = Decimal(total_notional)
        self._account = account

        self.rise_60s_threshold = rise_60s_threshold
        self.cooldown_seconds = int(cooldown_seconds)
        self.min_spike_rise = Decimal(str(min_spike_rise))
        self.retrace_frac = Decimal(str(retrace_frac))
        self.rise_low_lookback_ms = rise_low_lookback_hours * MS_PER_HOUR
        self.min_rise_duration_ms = min_rise_duration_hours * MS_PER_HOUR
        self.prior_high_lookback_minutes = prior_high_lookback_hours * 60
        self.prior_high_tolerance_percent = prior_high_tolerance_percent
        self.buy_ratio_entry_min = Decimal(str(buy_ratio_entry_min))
        self.stop_5m_high = bool(stop_5m_high)
        self.take_profit = Decimal(str(take_profit))
        self.max_hold_ms = int(max_hold_seconds) * MS_PER_SECOND
        self.wait_ms = int(wait_seconds) * MS_PER_SECOND

        self.bars_1s: deque[Bar1s] = deque()
        self.klines_1m: deque[Kline] = deque()
        self.klines_5m: deque[Kline] = deque()
        self.klines_15m: deque[Kline] = deque()
        self._kline_cache_time_ordered = {"1m": True, "5m": True, "15m": True}
        self._shared_feature_provider: SpikeSharedFeatureProvider | None = None

        self._trading_enabled = True
        self._entry_enabled = True
        self._execution_enabled = True

        self.last_signal_time: int | None = None
        self._pending: _PendingEntry | None = None
        self._last_bar_timestamp: int | None = None
        self._continuous_1s_count = 0

        # 持仓状态（on_fill 确认后填充）
        self.first_fill_time: int | None = None
        self.entry_price: Decimal | None = None
        self._spike_high: Decimal | None = None
        self._campaign_origin_price: Decimal | None = None
        self._pending_entry_meta: _PendingEntry | None = None
        self._active_campaign_id: str | None = None

        # candidate-v1 退出状态
        self.exit_strict_age_ms = (
            CandidateV1Config.strict_age_ms
            if exit_strict_age_ms is None
            else int(exit_strict_age_ms)
        )
        if self.exit_strict_age_ms <= 0:
            raise ValueError("exit_strict_age_ms must be positive")
        self.exit_flat_agreement = exit_flat_agreement
        self.time_risk_grace_ms = int(time_risk_grace_ms)
        self.time_risk_grace_loss_ratio = Decimal(str(time_risk_grace_loss_ratio))
        self.strong_strict_age_ms = strong_strict_age_ms
        self.weak_strict_age_ms = weak_strict_age_ms
        self.strong_bucket_strict_age_ms = strong_bucket_strict_age_ms
        self.weak_bucket_strict_age_ms = weak_bucket_strict_age_ms
        self.profit_unlock_ratio = (
            Decimal(str(profit_unlock_ratio))
            if profit_unlock_ratio is not None
            else None
        )
        self.profit_drawdown_ratio = (
            Decimal(str(profit_drawdown_ratio))
            if profit_drawdown_ratio is not None
            else None
        )
        self.profit_drawdown_peak_ratio = (
            Decimal(str(profit_drawdown_peak_ratio))
            if profit_drawdown_peak_ratio is not None
            else None
        )
        self.early_profit_unlock_ratio = (
            Decimal(str(early_profit_unlock_ratio))
            if early_profit_unlock_ratio is not None
            else None
        )
        self._candidate_exit_state = SpikeExitPolicyState()
        self._candidate_exit_config = CandidateV1Config(
            risk_start_ms=(
                self.exit_strict_age_ms
                if self.exit_flat_agreement is not None
                else CandidateV1Config.risk_start_ms
            ),
            medium_age_ms=(
                self.exit_strict_age_ms
                if self.exit_flat_agreement is not None
                else CandidateV1Config.medium_age_ms
            ),
            strict_age_ms=self.exit_strict_age_ms,
            flat_momentum_agreement=self.exit_flat_agreement,
            time_risk_grace_ms=self.time_risk_grace_ms,
            time_risk_grace_loss_ratio=self.time_risk_grace_loss_ratio,
            strong_strict_age_ms=self.strong_strict_age_ms,
            weak_strict_age_ms=self.weak_strict_age_ms,
            strong_bucket_strict_age_ms=self.strong_bucket_strict_age_ms,
            weak_bucket_strict_age_ms=self.weak_bucket_strict_age_ms,
            profit_unlock_ratio=self.profit_unlock_ratio,
            profit_drawdown_ratio=self.profit_drawdown_ratio,
            profit_drawdown_peak_ratio=self.profit_drawdown_peak_ratio,
        )
        self._early_profit_risk_unlocked = False
        self._candidate_peak_price: Decimal | None = None
        self._candidate_peak_1m_price: Decimal | None = None
        self._candidate_profit_unlocked = False
        self._candidate_drawdown_armed = False
        self._candidate_entry_bucket: str | None = None
        self._candidate_feature_config = CandidateFeatureConfig()
        self._candidate_features: CandidateFeatureSnapshot | None = None
        self._candidate_exit_waiting = False

        self._audit_events: List[StrategyAuditEvent] = []

    # ------------------------------------------------------------------
    # 引擎协议
    # ------------------------------------------------------------------

    def bind_account(self, account: StrategyAccount) -> None:
        self._account = account

    def bind_shared_feature_provider(
        self, provider: SpikeSharedFeatureProvider
    ) -> None:
        """在 sweep 开始前绑定同回放上下文的共享行情窗口。"""
        if self.bars_1s or self.klines_1m or self.klines_5m or self.klines_15m:
            raise RuntimeError("shared features must be bound before market events")
        self._shared_feature_provider = provider

    def set_trading_enabled(self, enabled: bool) -> None:
        self._trading_enabled = enabled

    def set_entry_enabled(self, enabled: bool) -> None:
        self._entry_enabled = enabled

    def set_execution_enabled(self, enabled: bool) -> None:
        self._execution_enabled = enabled

    def drain_audit_events(self) -> List[StrategyAuditEvent]:
        events = self._audit_events
        self._audit_events = []
        return events

    def on_kline(self, kline: Kline) -> List[OrderIntent]:
        if kline.interval == "1m":
            retained_minutes = max(
                30 * 60, self.rise_low_lookback_ms // MS_PER_MINUTE
            )
            cutoff = kline.close_time - retained_minutes * MS_PER_MINUTE
            self._append_kline_and_evict_expired(
                "1m", self.klines_1m, kline, cutoff
            )
        elif kline.interval == "5m":
            cutoff = kline.close_time - 40 * MS_PER_HOUR
            self._append_kline_and_evict_expired(
                "5m", self.klines_5m, kline, cutoff
            )
        elif kline.interval == "15m":
            cutoff = kline.close_time - 40 * MS_PER_HOUR
            self._append_kline_and_evict_expired(
                "15m", self.klines_15m, kline, cutoff
            )
        if self.first_fill_time is not None and self._trading_enabled:
            self.refresh_candidate_features()
        return []

    def _append_kline_and_evict_expired(
        self, interval: str, cache: deque[Kline], kline: Kline, cutoff: int
    ) -> None:
        self._kline_cache_time_ordered[interval] = append_kline_and_evict_expired(
            cache,
            kline,
            cutoff,
            is_time_ordered=self._kline_cache_time_ordered[interval],
        )

    def refresh_candidate_features(self) -> None:
        if self.first_fill_time is None:
            return
        if self._shared_feature_provider is not None:
            self._candidate_features = self._shared_feature_provider.candidate_features(
                self._candidate_feature_config
            )
        else:
            self._candidate_features = candidate_feature_snapshot(
                self.klines_1m,
                self.klines_5m,
                self.klines_15m,
                config=self._candidate_feature_config,
            )

    def on_fill(self, fill: Fill) -> None:
        if fill.symbol != self.symbol:
            return
        if fill.side == "SELL" and self.first_fill_time is None:
            meta = self._pending_entry_meta
            if meta is not None:
                self._active_campaign_id = _campaign_id(self.symbol, meta.signal_ms)
                self.first_fill_time = fill.fill_time
                self.entry_price = fill.price
                self._spike_high = meta.spike_high
                self._campaign_origin_price = meta.origin_price
                self._pending_entry_meta = None
                self._reset_candidate_state()
                self._record_audit(
                    fill.fill_time,
                    "pullback_entry_filled",
                    self._active_campaign_id,
                    {
                        "entry_price": str(fill.price),
                        "origin_price": str(meta.origin_price),
                        "spike_high": str(meta.spike_high),
                    },
                )
        elif fill.side == "BUY" and self.first_fill_time is not None:
            campaign_id = self._campaign_id_for_timing
            self._record_audit(
                fill.fill_time,
                "pullback_exit_filled",
                campaign_id,
                {"exit_price": str(fill.price), "quantity": str(fill.quantity)},
            )
            self.reconcile_position()

    def reconcile_position(self) -> None:
        if self.first_fill_time is None or self._account is None:
            return
        position = self._account.get_position(self.symbol)
        if position is None or position.quantity <= 0:
            self._clear_campaign_state()

    # ------------------------------------------------------------------
    # 1s 主流程
    # ------------------------------------------------------------------

    def on_bar1s(self, bar: Bar1s) -> List[OrderIntent]:
        self._update_cache(bar)

        if not self._trading_enabled or not self._execution_enabled:
            return []

        # 1. 持仓退出管理（candidate-v1 一致 + 5m 高点止损 + 止盈 + 超时）
        exit_intents = self._manage_exits(bar)
        if exit_intents:
            return exit_intents

        if len(self.bars_1s) < self.rise_window_bars:
            return []

        # 2. 待入场信号推进（大插针确认 + 回落接空）
        if self._pending is not None:
            entry_intents = self._advance_pending(bar)
            if entry_intents:
                return entry_intents

        # 3. 检测新的移动 60s 暴涨信号
        if self._entry_enabled:
            self._detect_60s_signal(bar)

        return []

    def _update_cache(self, bar: Bar1s) -> None:
        if (
            self._last_bar_timestamp is not None
            and bar.timestamp - self._last_bar_timestamp == MS_PER_SECOND
        ):
            self._continuous_1s_count += 1
        else:
            self._continuous_1s_count = 1
        self._last_bar_timestamp = bar.timestamp
        self.bars_1s.append(bar)
        while len(self.bars_1s) > self.rise_window_bars:
            self.bars_1s.popleft()

    def _record_audit(
        self, event_time: int, event_type: str, campaign_id: str | None, details: dict
    ) -> None:
        self._audit_events.append(
            StrategyAuditEvent(
                event_time=event_time,
                event_type=event_type,
                symbol=self.symbol,
                strategy_id=self.strategy_name,
                campaign_id=campaign_id,
                details=details,
            )
        )

    # ------------------------------------------------------------------
    # 入场：移动 60s 暴涨检测
    # ------------------------------------------------------------------

    def _detect_60s_signal(self, bar: Bar1s) -> None:
        bars = self.bars_1s
        cur = bars[-1]
        if (
            self.last_signal_time is not None
            and cur.timestamp - self.last_signal_time < self.cooldown_seconds * MS_PER_SECOND
        ):
            return
        if self._pending is not None:
            return
        if len(bars) != self.rise_window_bars:
            return
        window_base = bars[0]
        if (
            self._shared_feature_provider is None or self.rise_window_seconds != 60
        ) and self._continuous_1s_count < self.rise_window_bars:
            return
        if window_base.close <= 0:
            return
        # 默认 60s 窗口走共享提供器固定 60s 特征（优化路径）；自定义窗口用本地计算。
        if (
            self._shared_feature_provider is not None
            and self.rise_window_seconds == 60
        ):
            shared_features = self._shared_feature_provider.bar_features(bar)
            if (
                shared_features is None
                or not shared_features.continuous_60s
                or shared_features.rise_60s is None
            ):
                return
            rise_window = shared_features.rise_60s
        else:
            rise_window = cur.close / window_base.close - Decimal("1")
        if rise_window < self.rise_60s_threshold:
            return
        if not self._rise_duration_ok(cur.timestamp):
            return

        minute_start = cur.timestamp - (cur.timestamp % MS_PER_MINUTE)
        prior_high_point = self._prior_high_point(minute_start)
        if self.prior_high_lookback_minutes > 0 and prior_high_point is None:
            return

        self.last_signal_time = cur.timestamp
        self._pending = _PendingEntry(
            signal_ms=cur.timestamp,
            origin_price=window_base.close,
            spike_high=cur.high,
            prior_high=(
                prior_high_point[0] if prior_high_point is not None else None
            ),
            prior_high_time=(
                prior_high_point[1] if prior_high_point is not None else None
            ),
        )
        self._record_audit(
            cur.timestamp,
            "signal_triggered",
            _campaign_id(self.symbol, cur.timestamp),
            {
                "rise_60s": str(rise_window),
                "rise_window": str(rise_window),
                "rise_window_seconds": self.rise_window_seconds,
                "origin_price": str(window_base.close),
                "prior_high": (
                    str(prior_high_point[0])
                    if prior_high_point is not None
                    else None
                ),
                "prior_high_time": (
                    prior_high_point[1] if prior_high_point is not None else None
                ),
            },
        )

    def _rise_duration_ok(self, signal_ms: int) -> bool:
        """6h 起涨背景：过去 rise_low_lookback 窗口内最低 1m 低点距信号
        >= min_rise_duration（该低点在更早之前，上涨已持续足够久）。"""
        lookback_ms = self.rise_low_lookback_ms
        if lookback_ms <= 0:
            return True
        minute_start = signal_ms - (signal_ms % MS_PER_MINUTE)
        minutes = lookback_ms // MS_PER_MINUTE
        if (
            self._shared_feature_provider is not None
            and self._shared_feature_provider.supports_metric(
                SPIKE_MIN_LOW_1M_FEATURE
            )
        ):
            point = self._shared_feature_provider.min_low_point_1m(
                minute_start, minutes
            )
            if point is None:
                return False
            return minute_start - point[1] >= self.min_rise_duration_ms

        window = self._completed_1m_window(minute_start, minutes)
        if not window:
            return False
        low_k = min(window, key=lambda k: k.low)
        return minute_start - low_k.open_time >= self.min_rise_duration_ms

    def _completed_1m_window(
        self, minute_start: int, minutes: int
    ) -> tuple[Kline, ...]:
        """返回连续完整的 1m 窗口；缺任一分钟时返回空元组。"""
        window_start = minute_start - minutes * MS_PER_MINUTE
        by_open_time = {
            k.open_time: k
            for k in self.klines_1m
            if window_start <= k.open_time < minute_start
        }
        expected_times = range(window_start, minute_start, MS_PER_MINUTE)
        if any(open_time not in by_open_time for open_time in expected_times):
            return ()
        return tuple(by_open_time[open_time] for open_time in expected_times)

    def _prior_high_point(self, minute_start: int) -> tuple[Decimal, int] | None:
        """返回信号所在分钟之前完整 1m K 线窗口的前高。"""
        if self.prior_high_lookback_minutes <= 0:
            return None
        if (
            self._shared_feature_provider is not None
            and self._shared_feature_provider.supports_metric(
                SPIKE_PRIOR_HIGH_1M_FEATURE
            )
        ):
            return self._shared_feature_provider.prior_high_point_1m(
                minute_start,
                self.prior_high_lookback_minutes,
            )
        completed = self._completed_1m_window(
            minute_start, self.prior_high_lookback_minutes
        )
        if not completed:
            return None
        point = max(completed, key=lambda item: (item.high, item.open_time))
        return point.high, point.open_time

    def _buy_ratio_entry(self, bar: Bar1s) -> Decimal | None:
        """接空前 10 秒主动买占比（不含当前触发 bar）。"""
        window = list(self.bars_1s)[-11:-1]
        if len(window) < 10:
            return None
        total_buy = Decimal("0")
        total_sell = Decimal("0")
        for b in window:
            if b.taker_buy_volume is None or b.taker_sell_volume is None:
                return None
            total_buy += b.taker_buy_volume
            total_sell += b.taker_sell_volume
        total = total_buy + total_sell
        if total <= 0:
            return None
        return total_buy / total

    # ------------------------------------------------------------------
    # 入场：回落接空
    # ------------------------------------------------------------------

    def _advance_pending(self, bar: Bar1s) -> List[OrderIntent]:
        p = self._pending
        elapsed_ms = bar.timestamp - p.signal_ms
        if elapsed_ms > self.wait_ms:
            timeout_stage = (
                "prior_high_not_cleared"
                if p.prior_high_blocked
                else "retrace_not_reached"
            )
            self._record_audit(
                bar.timestamp,
                "signal_expired",
                _campaign_id(self.symbol, p.signal_ms),
                {
                    "reason": "pullback_timeout",
                    "wait_ms": self.wait_ms,
                    "timeout_stage": timeout_stage,
                    "retrace_reached": p.retrace_reached,
                },
            )
            self._pending = None
            return []
        if bar.low < p.origin_price:
            self._record_audit(
                bar.timestamp,
                "signal_invalidated",
                _campaign_id(self.symbol, p.signal_ms),
                {
                    "reason": "origin_breached",
                    "origin_price": str(p.origin_price),
                    "bar_low": str(bar.low),
                },
            )
            self._pending = None
            return []
        if bar.high > p.spike_high:
            p.spike_high = bar.high
        if p.spike_high < p.origin_price * (1 + self.min_spike_rise):
            return []
        candidate = p.spike_high - self.retrace_frac * (
            p.spike_high - p.origin_price
        )
        if candidate <= p.origin_price or bar.low > candidate:
            return []
        p.retrace_reached = True
        if p.prior_high is not None:
            allowed_prior_high = p.prior_high * (
                Decimal("1")
                - self.prior_high_tolerance_percent / Decimal("100")
            )
            if (
                candidate < allowed_prior_high
                or (
                    self.prior_high_tolerance_percent == 0
                    and candidate == allowed_prior_high
                )
            ):
                p.prior_high_blocked = True
                return []
        buy_ratio = self._buy_ratio_entry(bar)
        if buy_ratio is not None and buy_ratio < self.buy_ratio_entry_min:
            self._record_audit(
                bar.timestamp,
                "pullback_rejected_low_buy_ratio",
                _campaign_id(self.symbol, p.signal_ms),
                {"buy_ratio_entry": str(buy_ratio)},
            )
            self._pending = None
            return []
        entry_price = max(candidate, bar.open)
        if self._account is not None and self._account.has_open_position(self.symbol):
            self._pending = None
            return []
        quantity = self.total_notional / entry_price
        self._pending_entry_meta = p
        self._pending = None
        self._record_audit(
            bar.timestamp,
            "pullback_entry_placed",
            _campaign_id(self.symbol, p.signal_ms),
            {
                "candidate": str(candidate),
                "entry_price": str(entry_price),
                "spike_high": str(p.spike_high),
                "origin_price": str(p.origin_price),
                "retrace_frac": str(self.retrace_frac),
                "prior_high": (
                    str(p.prior_high) if p.prior_high is not None else None
                ),
                "prior_high_time": p.prior_high_time,
                "buy_ratio_entry": (
                    str(buy_ratio) if buy_ratio is not None else None
                ),
            },
        )
        return [
            OrderIntent(
                symbol=self.symbol,
                side="SELL",
                price=entry_price,
                quantity=quantity,
                client_order_id=_build_entry_client_order_id(
                    self.symbol, p.signal_ms
                ),
                order_type="MARKET",
                strategy_id=self.strategy_name,
                trigger_reason="pullback_entry",
                campaign_id=_campaign_id(self.symbol, p.signal_ms),
            )
        ]

    # ------------------------------------------------------------------
    # 退出：candidate-v1 一致 + 5m 高点止损 + 止盈 + 超时
    # ------------------------------------------------------------------

    @property
    def _campaign_id_for_timing(self) -> str | None:
        return self._active_campaign_id

    def _clear_campaign_state(self) -> None:
        self.first_fill_time = None
        self.entry_price = None
        self._spike_high = None
        self._campaign_origin_price = None
        self._pending_entry_meta = None
        self._active_campaign_id = None
        self._reset_candidate_state()

    def _reset_candidate_state(self) -> None:
        self._candidate_exit_state = SpikeExitPolicyState()
        self._early_profit_risk_unlocked = False
        self._candidate_peak_price = None
        self._candidate_peak_1m_price = None
        self._candidate_profit_unlocked = False
        self._candidate_drawdown_armed = False
        self._candidate_entry_bucket = None
        self._candidate_features = None
        self._candidate_exit_waiting = False

    def _manage_exits(self, bar: Bar1s) -> List[OrderIntent]:
        if self.first_fill_time is None or self._account is None:
            return []
        position = self._account.get_position(self.symbol)
        if position is None or position.side != "SHORT" or position.quantity <= 0:
            return []
        elapsed_ms = bar.available_time - self.first_fill_time
        if elapsed_ms < 0:
            return []
        if self._candidate_exit_state.exit_requested:
            return []

        # 5m 插针高点止损（持仓>5min 重新触及插针高点，市价止损）
        if (
            self.stop_5m_high
            and self._spike_high is not None
            and elapsed_ms > 5 * MS_PER_MINUTE
            and bar.high >= self._spike_high
        ):
            return [
                self._exit_intent(
                    bar.high, "stop_5m_high", bar.available_time, position.quantity
                )
            ]

        # candidate-v1 一致退出
        candidate = self._candidate_exit(bar.available_time, bar.close, position)
        if candidate:
            return candidate

        # 10% 硬止盈
        if self.entry_price is not None and self.take_profit > 0:
            tp_price = self.entry_price * (1 - self.take_profit)
            if bar.low <= tp_price:
                return [
                    self._exit_intent(
                        tp_price, "take_profit", bar.available_time, position.quantity
                    )
                ]

        # 1h 超时兜底
        if elapsed_ms >= self.max_hold_ms:
            return [
                self._exit_intent(
                    bar.close, "timeout", bar.available_time, position.quantity
                )
            ]
        return []

    def _exit_intent(
        self, price: Decimal, reason: str, event_time: int, quantity: Decimal
    ) -> OrderIntent:
        self._record_audit(
            event_time,
            "pullback_exit_requested",
            self._campaign_id_for_timing,
            {"reason": reason, "price": str(price), "quantity": str(quantity)},
        )
        reason_code = {
            "candidate_origin_reduce": "h",
            "candidate_time_risk_exit": "t",
            "candidate_momentum_exit": "t",
            "candidate_profit_drawdown_exit": "r",
            "candidate_trend_exit": "c",
            "candidate_gate_stop": "t",
            "stop_5m_high": "c",
            "take_profit": "r",
            "timeout": "t",
        }.get(reason, "c")
        return OrderIntent(
            symbol=self.symbol,
            side="BUY",
            price=price,
            quantity=quantity,
            client_order_id=build_exit_client_order_id(
                self.symbol, event_time, reason_code
            ),
            order_type="MARKET",
            reduce_only=True,
            strategy_id=self.strategy_name,
            trigger_reason=reason,
            campaign_id=self._active_campaign_id,
        )

    def _candidate_exit(
        self, event_time: int, mark_price: Decimal, position
    ) -> List[OrderIntent]:
        """与 v1 candidate-v1 一致的退出评估。"""
        if self._campaign_origin_price is None or self._candidate_features is None:
            return []
        features = self._candidate_features
        elapsed_ms = event_time - self.first_fill_time
        if elapsed_ms < 0:
            return []
        net_pnl = (
            (position.entry_price - mark_price) * position.quantity
            - position.total_commission
        )
        if self._candidate_peak_price is None or mark_price < self._candidate_peak_price:
            self._candidate_peak_price = mark_price
        unlock_ratio = self._candidate_exit_config.profit_unlock_ratio
        if (
            not self._candidate_profit_unlocked
            and unlock_ratio is not None
            and self._candidate_peak_price is not None
            and position.entry_price > 0
            and (position.entry_price - self._candidate_peak_price)
            / position.entry_price
            >= unlock_ratio
        ):
            self._candidate_profit_unlocked = True
        profit_drawdown = False
        drawdown_ratio = self._candidate_exit_config.profit_drawdown_ratio
        peak_ratio = self._candidate_exit_config.profit_drawdown_peak_ratio
        last_1m_close = None
        if self.klines_1m:
            k = self.klines_1m[-1]
            if k.open_time >= self.first_fill_time:
                last_1m_close = k.close
        if last_1m_close is not None:
            if (
                self._candidate_peak_1m_price is None
                or last_1m_close < self._candidate_peak_1m_price
            ):
                self._candidate_peak_1m_price = last_1m_close
        if (
            drawdown_ratio is not None
            and self._candidate_peak_1m_price is not None
            and last_1m_close is not None
        ):
            if peak_ratio is not None:
                if (
                    not self._candidate_drawdown_armed
                    and position.entry_price > 0
                    and (position.entry_price - self._candidate_peak_1m_price)
                    / position.entry_price
                    >= peak_ratio
                ):
                    self._candidate_drawdown_armed = True
                unlocked = self._candidate_drawdown_armed
                peak_price = self._candidate_peak_1m_price
                current_price = last_1m_close
            else:
                unlocked = unlock_ratio is None or self._candidate_profit_unlocked
                peak_price = self._candidate_peak_price
                current_price = mark_price
            profit_drawdown = (
                unlocked
                and peak_price is not None
                and peak_price > 0
                and (current_price - peak_price) / peak_price >= drawdown_ratio
            )
        gross_return = (position.entry_price - mark_price) / position.entry_price
        if (
            not self._early_profit_risk_unlocked
            and self.early_profit_unlock_ratio is not None
            and gross_return > self.early_profit_unlock_ratio
        ):
            self._early_profit_risk_unlocked = True
            self._candidate_exit_state.min_risk_age_ms = 0
        risk_elapsed_ms = (
            max(elapsed_ms, self._candidate_exit_config.risk_start_ms)
            if self._early_profit_risk_unlocked
            else elapsed_ms
        )
        time_risk, momentum_risk = candidate_v1_risks(
            elapsed_ms=risk_elapsed_ms,
            decay_agreement=features.decay_agreement,
            net_pnl=net_pnl,
            down_channel_5m=features.down_channel_5m,
            down_channel_15m=features.down_channel_15m,
            config=self._candidate_exit_config,
            notional=self.total_notional,
            profit_unlocked=self._candidate_profit_unlocked,
            entry_bucket=self._candidate_entry_bucket,
        )
        # 最短持有期：入场后 age 内即使通道站稳破位也不退出。
        # 防止插针回落后"早已点亮的站稳"在首根 K 线就踢出（假破位早退）。
        gate_breakout = elapsed_ms < self.exit_stable_breakout_age_ms
        # 只有实际存在被 age gate 挡住的 stable breakout 时才启用风险闸。
        gate_stop = (
            gate_breakout
            and (features.stable_breakout_5m or features.stable_breakout_15m)
            and self.stable_breakout_gate_stop_pct is not None
            and self.entry_price is not None
            and self.entry_price > 0
            and (mark_price - self.entry_price) / self.entry_price
            >= self.stable_breakout_gate_stop_pct
        )
        observation = ExitObservation(
            event_time=event_time,
            first_fill_time=self.first_fill_time,
            price=mark_price,
            origin_price=self._campaign_origin_price,
            decay_agreement=features.decay_agreement,
            time_risk=time_risk,
            momentum_risk=momentum_risk,
            profit_drawdown=profit_drawdown,
            gate_stop=gate_stop,
            stable_breakout_5m=(
                False if gate_breakout else features.stable_breakout_5m
            ),
            stable_breakout_15m=(
                False if gate_breakout else features.stable_breakout_15m
            ),
        )
        decision = self._candidate_exit_state.evaluate(observation)
        if decision.action == ExitAction.HOLD:
            return []
        reduce_half = decision.action == ExitAction.REDUCE_HALF
        quantity = position.quantity / 2 if reduce_half else position.quantity
        reason = (
            "candidate_origin_reduce"
            if reduce_half
            else {
                "time_risk": "candidate_time_risk_exit",
                "momentum_risk": "candidate_momentum_exit",
                "profit_drawdown": "candidate_profit_drawdown_exit",
                "gate_stop": "candidate_gate_stop",
            }.get(decision.reason, "candidate_trend_exit")
        )
        if not reduce_half:
            self._candidate_exit_state.exit_requested = True
        return [
            self._exit_intent(mark_price, reason, event_time, quantity),
        ]


class PullbackV3BacktestStrategy:
    """多币种适配器，符合 backtest.engine.Strategy 协议。"""

    def __init__(
        self,
        symbols: List[str],
        total_notional: Decimal,
        account: Optional[StrategyAccount] = None,
        strategy_class: type[PullbackV3Strategy] = PullbackV3Strategy,
        strategy_parameters: dict[str, object] | None = None,
    ):
        params = strategy_parameters or {}
        self.strategies = {
            symbol: strategy_class(
                symbol,
                total_notional=total_notional,
                account=account,
                **params,
            )
            for symbol in symbols
        }
        self._account = account
        self._entry_enabled = True

    def bind_account(self, account: StrategyAccount) -> None:
        self._account = account
        for strategy in self.strategies.values():
            strategy.bind_account(account)

    def bind_shared_feature_provider(
        self, provider: SpikeSharedFeatureProvider
    ) -> None:
        for strategy in self.strategies.values():
            strategy.bind_shared_feature_provider(provider)

    def set_trading_enabled(self, enabled: bool) -> None:
        for strategy in self.strategies.values():
            strategy.set_trading_enabled(enabled)

    def set_entry_enabled(self, enabled: bool) -> None:
        self._entry_enabled = enabled
        for strategy in self.strategies.values():
            strategy.set_entry_enabled(enabled)

    def set_execution_enabled(self, enabled: bool) -> None:
        for strategy in self.strategies.values():
            strategy.set_execution_enabled(enabled)

    def on_bar1s(self, bar: Bar1s) -> List[OrderIntent]:
        strategy = self.strategies.get(bar.symbol)
        return strategy.on_bar1s(bar) if strategy else []

    def on_kline(self, kline: Kline) -> List[OrderIntent]:
        strategy = self.strategies.get(kline.symbol)
        return strategy.on_kline(kline) if strategy else []

    def on_fill(self, fill: Fill) -> None:
        strategy = self.strategies.get(fill.symbol)
        if strategy is not None:
            strategy.on_fill(fill)

    def reconcile_position(self, symbol: str) -> None:
        strategy = self.strategies.get(symbol)
        if strategy is not None:
            strategy.reconcile_position()

    def drain_audit_events(self) -> List[StrategyAuditEvent]:
        events: List[StrategyAuditEvent] = []
        for strategy in self.strategies.values():
            events.extend(strategy.drain_audit_events())
        return events


class PullbackV3:
    """Spike v3 声明：移动 60s 暴涨后等待回落接空。"""

    name = "pullback-v3"
    strategy_class = PullbackV3Strategy
    shared_feature_provider = SpikeSharedFeatureProvider
    data_requirements = SpikeDataRequirements(
        market_timeframes=("1s", "1m", "5m", "15m"),
        shared_features=frozenset({
            SPIKE_RISE_60S_FEATURE,
            SPIKE_CANDIDATE_EXIT_FEATURE,
            SPIKE_MIN_LOW_1M_FEATURE,
            SPIKE_PRIOR_HIGH_1M_FEATURE,
        }),
        shared_metrics=SPIKE_V2_SHARED_METRICS,
        metrics_5m=False,
    )
    defaults = SpikeStrategyDefaults(
        exit_policy="candidate-v1",
        prior_high_lookback_hours=4,
        rise_low_lookback_hours=24,
        min_rise_duration_hours=6,
        entry_tier_mode="three-tier",
        profit_unlock_percent=None,
    )
    supported_parameters = frozenset(
        {
            "rise_60s_threshold",
            "rise_window_seconds",
            "cooldown_seconds",
            "min_spike_rise",
            "retrace_frac",
            "rise_low_lookback_hours",
            "min_rise_duration_hours",
            "prior_high_lookback_hours",
            "prior_high_tolerance_percent",
            "buy_ratio_entry_min",
            "exit_strict_age_ms",
            "exit_flat_agreement",
            "time_risk_grace_ms",
            "time_risk_grace_loss_ratio",
            "strong_strict_age_ms",
            "weak_strict_age_ms",
            "strong_bucket_strict_age_ms",
            "weak_bucket_strict_age_ms",
            "profit_unlock_ratio",
            "profit_drawdown_ratio",
            "profit_drawdown_peak_ratio",
            "early_profit_unlock_ratio",
            "exit_stable_breakout_age_ms",
            "stable_breakout_gate_stop_pct",
            "stop_5m_high",
            "take_profit",
            "max_hold_seconds",
            "wait_seconds",
        }
    )
    internal_parameters = frozenset()
