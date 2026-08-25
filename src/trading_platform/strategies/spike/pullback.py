"""
Spike v3 全新一套：大插针(>=30%)回落接空做空。

与 v1/v2 的区别（独立实现，不继承 DynamicSpikeShortStrategy）：
- 入场不预测冲高、不挂三档 ATR 限价单；改为等价格回吐插针涨幅后回落接空。
  信号 = 3s 暴涨(>=3% + 放量 2x) + 6h 起涨背景(过去 24h 最低 1m 低点距信号>=6h)
        + 插针总涨幅>=30%(spike_high/origin-1) + 价格回吐 retrace_frac 涨幅
        + 买卖比轻过滤(接空前 10s 主动买占比 >= buy_ratio_entry_min)。
  入场以市价单模拟"回落触及成交"，成交价=max(candidate, bar.open)。
- 退出与 v1 一致（candidate-v1 状态机：动量衰减/时间风险/浮盈回撤/通道突破），
  额外增加 5m 插针高点止损（持仓>5min 重新触及插针高点即平）、10% 硬止盈、1h 超时兜底。
- 入场后 spike_high 冻结在入场时刻，用于 5m 插针高点止损。

研究对象: tools/research_pullback_short.py
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, List, Optional

from trading_platform.shared.events import (
    Bar1s,
    Fill,
    Kline,
    OrderIntent,
    StrategyAuditEvent,
)
from trading_platform.shared.execution import StrategyAccount
from trading_platform.strategies.spike.definition import (
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
    append_kline_and_evict_expired,
)
from trading_platform.strategies.spike.short import build_exit_client_order_id

MS_PER_SECOND = 1000
MS_PER_MINUTE = 60 * MS_PER_SECOND
MS_PER_HOUR = 3600 * MS_PER_SECOND

# 入场检测需要的 1s Bar 数量：索引 i-60 .. i
BAR_BUFFER = 65


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
    """已检测到 3s 暴涨、等待大插针确认与回落接空的待入场状态。"""

    signal_ms: int
    origin_price: Decimal
    spike_high: Decimal


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
        rise_3s_threshold: Decimal = Decimal("0.03"),
        vol_multiple: Decimal = Decimal("2.0"),
        cooldown_seconds: int = 180,
        min_spike_rise: Decimal = Decimal("0.40"),
        retrace_frac: Decimal = Decimal("0.30"),
        rise_low_lookback_hours: int = 24,
        min_rise_duration_hours: int = 6,
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
        # 额外退出
        stop_5m_high: bool = True,
        take_profit: Decimal = Decimal("0.15"),
        max_hold_seconds: int = 3600,
        wait_seconds: int = 3600,
    ):
        if total_notional is None or total_notional <= 0:
            raise ValueError("total_notional must be a positive Decimal")
        if min_rise_duration_hours > rise_low_lookback_hours:
            raise ValueError(
                "min_rise_duration_hours must not exceed rise_low_lookback_hours"
            )
        self.symbol = symbol
        self.total_notional = Decimal(total_notional)
        self._account = account

        self.rise_3s_threshold = Decimal(rise_3s_threshold)
        self.vol_multiple = Decimal(vol_multiple)
        self.cooldown_seconds = int(cooldown_seconds)
        self.min_spike_rise = Decimal(min_spike_rise)
        self.retrace_frac = Decimal(retrace_frac)
        self.rise_low_lookback_ms = int(rise_low_lookback_hours) * MS_PER_HOUR
        self.min_rise_duration_ms = int(min_rise_duration_hours) * MS_PER_HOUR
        self.buy_ratio_entry_min = Decimal(buy_ratio_entry_min)
        self.stop_5m_high = bool(stop_5m_high)
        self.take_profit = Decimal(take_profit)
        self.max_hold_ms = int(max_hold_seconds) * MS_PER_SECOND
        self.wait_ms = int(wait_seconds) * MS_PER_SECOND

        self.bars_1s: deque[Bar1s] = deque()
        self.klines_1m: deque[Kline] = deque()
        self.klines_5m: deque[Kline] = deque()
        self.klines_15m: deque[Kline] = deque()
        self._kline_cache_time_ordered = {"1m": True, "5m": True, "15m": True}

        self._trading_enabled = True
        self._entry_enabled = True
        self._execution_enabled = True

        self.last_signal_time: int | None = None
        self._pending: _PendingEntry | None = None

        # 持仓状态（on_fill 确认后填充）
        self.first_fill_time: int | None = None
        self.entry_price: Decimal | None = None
        self._spike_high: Decimal | None = None
        self._campaign_origin_price: Decimal | None = None
        self._pending_entry_meta: _PendingEntry | None = None

        # candidate-v1 退出状态
        self.exit_strict_age_ms = exit_strict_age_ms
        self.exit_flat_agreement = exit_flat_agreement
        self.time_risk_grace_ms = int(time_risk_grace_ms)
        self.time_risk_grace_loss_ratio = Decimal(time_risk_grace_loss_ratio)
        self.strong_strict_age_ms = strong_strict_age_ms
        self.weak_strict_age_ms = weak_strict_age_ms
        self.strong_bucket_strict_age_ms = strong_bucket_strict_age_ms
        self.weak_bucket_strict_age_ms = weak_bucket_strict_age_ms
        self.profit_unlock_ratio = profit_unlock_ratio
        self.profit_drawdown_ratio = profit_drawdown_ratio
        self.profit_drawdown_peak_ratio = profit_drawdown_peak_ratio
        self.early_profit_unlock_ratio = (
            Decimal(early_profit_unlock_ratio)
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
                self.first_fill_time = fill.fill_time
                self.entry_price = fill.price
                self._spike_high = meta.spike_high
                self._campaign_origin_price = meta.origin_price
                self._pending_entry_meta = None
                self._reset_candidate_state()
                self._record_audit(
                    fill.fill_time,
                    "pullback_entry_filled",
                    _campaign_id(self.symbol, meta.signal_ms),
                    {
                        "entry_price": str(fill.price),
                        "origin_price": str(meta.origin_price),
                        "spike_high": str(meta.spike_high),
                    },
                )
        elif fill.side == "BUY" and self.first_fill_time is not None:
            self._record_audit(
                fill.fill_time,
                "pullback_exit_filled",
                self._campaign_id_for_timing,
                {"exit_price": str(fill.price), "quantity": str(fill.quantity)},
            )

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

        if len(self.bars_1s) < BAR_BUFFER:
            return []

        # 2. 待入场信号推进（大插针确认 + 回落接空）
        if self._pending is not None:
            entry_intents = self._advance_pending(bar)
            if entry_intents:
                return entry_intents

        # 3. 检测新 3s 暴涨信号
        if self._entry_enabled:
            self._detect_3s_signal(bar)

        return []

    def _update_cache(self, bar: Bar1s) -> None:
        self.bars_1s.append(bar)
        while len(self.bars_1s) > BAR_BUFFER:
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
    # 入场：3s 暴涨检测
    # ------------------------------------------------------------------

    def _detect_3s_signal(self, bar: Bar1s) -> None:
        bars = self.bars_1s
        cur = bars[-1]
        if (
            self.last_signal_time is not None
            and cur.timestamp - self.last_signal_time < self.cooldown_seconds * MS_PER_SECOND
        ):
            return
        if self._pending is not None:
            return
        b3 = bars[-4]
        b60 = bars[-61]
        if cur.timestamp - b3.timestamp != 3 * MS_PER_SECOND:
            return
        if cur.timestamp - b60.timestamp != 60 * MS_PER_SECOND:
            return
        if b3.close <= 0:
            return
        rise_3s = cur.close / b3.close - 1
        if rise_3s < self.rise_3s_threshold:
            return
        window = list(bars)[-60:]
        median_volume = sorted(b.volume for b in window)[len(window) // 2]
        if median_volume <= 0:
            return
        vol_3s = sum(b.volume for b in list(bars)[-3:])
        if vol_3s < self.vol_multiple * median_volume * 3:
            return
        if not self._rise_duration_ok(cur.timestamp):
            return

        self.last_signal_time = cur.timestamp
        self._pending = _PendingEntry(
            signal_ms=cur.timestamp,
            origin_price=b3.close,
            spike_high=cur.high,
        )
        self._record_audit(
            cur.timestamp,
            "signal_triggered",
            _campaign_id(self.symbol, cur.timestamp),
            {
                "rise_3s": str(rise_3s),
                "volume_multiple_3s": str(vol_3s / median_volume),
                "origin_price": str(b3.close),
            },
        )

    def _rise_duration_ok(self, signal_ms: int) -> bool:
        """6h 起涨背景：过去 rise_low_lookback 窗口内最低 1m 低点距信号
        >= min_rise_duration（该低点在更早之前，上涨已持续足够久）。"""
        lookback_ms = self.rise_low_lookback_ms
        if lookback_ms <= 0:
            return True
        window = [
            k
            for k in self.klines_1m
            if k.open_time >= signal_ms - lookback_ms and k.open_time < signal_ms
        ]
        if len(window) < 2:
            return False
        low_k = min(window, key=lambda k: k.low)
        return signal_ms - low_k.open_time >= self.min_rise_duration_ms

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
        if elapsed_ms > self.wait_ms or bar.low < p.origin_price:
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
        if self.first_fill_time is None:
            return None
        signal_ms = self.first_fill_time
        return _campaign_id(self.symbol, signal_ms)

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
        observation = ExitObservation(
            event_time=event_time,
            first_fill_time=self.first_fill_time,
            price=mark_price,
            origin_price=self._campaign_origin_price,
            decay_agreement=features.decay_agreement,
            time_risk=time_risk,
            momentum_risk=momentum_risk,
            profit_drawdown=profit_drawdown,
            stable_breakout_5m=features.stable_breakout_5m,
            stable_breakout_15m=features.stable_breakout_15m,
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

    def drain_audit_events(self) -> List[StrategyAuditEvent]:
        events: List[StrategyAuditEvent] = []
        for strategy in self.strategies.values():
            events.extend(strategy.drain_audit_events())
        return events


class PullbackV3:
    """Spike v3 冻结声明：大插针(>=30%)回落接空。"""

    name = "pullback-v3"
    strategy_class = PullbackV3Strategy
    shared_feature_provider = None
    data_requirements = SpikeDataRequirements(
        market_timeframes=("1s", "1m", "5m", "15m"), metrics_5m=False
    )
    defaults = SpikeStrategyDefaults(
        exit_policy="candidate-v1",
        prior_high_lookback_hours=0,
        rise_low_lookback_hours=24,
        min_rise_duration_hours=6,
        entry_tier_mode="three-tier",
        profit_unlock_percent=None,
    )
    supported_parameters = frozenset(
        {
            "rise_3s_threshold",
            "vol_multiple",
            "cooldown_seconds",
            "min_spike_rise",
            "retrace_frac",
            "rise_low_lookback_hours",
            "min_rise_duration_hours",
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
            "stop_5m_high",
            "take_profit",
            "max_hold_seconds",
            "wait_seconds",
        }
    )
    internal_parameters = frozenset()
