"""Spike v2.1 策略声明：v2 基线加连阳、OI 和多空比研究能力。"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import numpy as np

from trading_platform.strategies.spike.definition import (
    SpikeDataRequirements,
    SpikeStrategyDefaults,
)
from trading_platform.strategies.spike.entry_features import entry_context_features
from trading_platform.strategies.spike.v2 import V2
from trading_platform.shared.events import OrderIntent
from trading_platform.strategies.spike.short import (
    DynamicSpikeShortStrategy,
    build_exit_client_order_id,
)
from trading_platform.strategies.spike.scoring import (
    compute_score,
    premium_pct as premium_pct_value,
)


class SpikeV21Strategy(DynamicSpikeShortStrategy):
    strategy_name = "v2.1"

    def __init__(
        self,
        *args,
        max_consecutive_up_minutes: int = 0,
        max_oi_change_pct: float = 0.0,
        max_ls_ratio: float = 0.0,
        min_td_sell_setup_5m: int = 0,
        min_volume_multiple_5m: Decimal = Decimal("0"),
        metrics_series: list[tuple[int, float, float]] | None = None,
        group_rise_12h_threshold: float = 0.0,
        loose_consecutive_up_minutes: int = 0,
        loose_max_ls_ratio: float | None = None,
        strong_tier_atr_shift: float = 0.0,
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
        entry_premium_mult: float = 0.0,
        entry_premium_floor: float = 3.0,
        entry_premium_cap: float = 35.0,
        entry_premium_model: str | None = None,
        entry_scoring_enabled: bool = False,
        entry_scoring_threshold: float = 0.5,
        entry_scoring_config: str | None = None,
        entry_premium_base_pct: float = 1.0,
        oi_stop_enabled: bool = False,
        oi_stop_oi_rise_pct: float = 5.0,
        oi_stop_loss_pct: float = 3.0,
        **kwargs,
    ):
        super().__init__(
            *args,
            exit_strict_age_ms=exit_strict_age_ms,
            exit_flat_agreement=exit_flat_agreement,
            time_risk_grace_ms=time_risk_grace_ms,
            time_risk_grace_loss_ratio=time_risk_grace_loss_ratio,
            strong_strict_age_ms=strong_strict_age_ms,
            weak_strict_age_ms=weak_strict_age_ms,
            strong_bucket_strict_age_ms=strong_bucket_strict_age_ms,
            weak_bucket_strict_age_ms=weak_bucket_strict_age_ms,
            profit_unlock_ratio=profit_unlock_ratio,
            profit_drawdown_ratio=profit_drawdown_ratio,
            profit_drawdown_peak_ratio=profit_drawdown_peak_ratio,
            **kwargs,
        )
        if max_consecutive_up_minutes < 0:
            raise ValueError("max_consecutive_up_minutes must not be negative")
        self.max_consecutive_up_minutes = int(max_consecutive_up_minutes)
        self.max_oi_change_pct = float(max_oi_change_pct)
        self.max_ls_ratio = float(max_ls_ratio)
        self.min_td_sell_setup_5m = int(min_td_sell_setup_5m)
        self.min_volume_multiple_5m = Decimal(str(min_volume_multiple_5m))
        if not 0 <= self.min_td_sell_setup_5m <= 9:
            raise ValueError("min_td_sell_setup_5m must be between 0 and 9")
        if self.min_volume_multiple_5m < 0:
            raise ValueError("min_volume_multiple_5m must not be negative")
        if group_rise_12h_threshold < 0 or loose_consecutive_up_minutes < 0:
            raise ValueError(
                "group_rise_12h_threshold and loose_consecutive_up_minutes "
                "must not be negative"
            )
        if loose_max_ls_ratio is not None and loose_max_ls_ratio < 0:
            raise ValueError("loose_max_ls_ratio must not be negative")
        if strong_tier_atr_shift < 0:
            raise ValueError("strong_tier_atr_shift must not be negative")
        self.group_rise_12h_threshold = float(group_rise_12h_threshold)
        self.loose_consecutive_up_minutes = int(loose_consecutive_up_minutes)
        self.loose_max_ls_ratio = (
            float(loose_max_ls_ratio)
            if loose_max_ls_ratio is not None
            else None
        )
        self.strong_tier_atr_shift = float(strong_tier_atr_shift)
        if exit_strict_age_ms is not None and exit_strict_age_ms <= 0:
            raise ValueError("exit_strict_age_ms must be positive")
        if exit_flat_agreement is not None and not 1 <= exit_flat_agreement <= 3:
            raise ValueError("exit_flat_agreement must be between 1 and 3")
        self.exit_strict_age_ms = (
            int(exit_strict_age_ms) if exit_strict_age_ms is not None else None
        )
        self.exit_flat_agreement = (
            int(exit_flat_agreement) if exit_flat_agreement is not None else None
        )
        if time_risk_grace_ms < 0:
            raise ValueError("time_risk_grace_ms must not be negative")
        if not 0 < time_risk_grace_loss_ratio <= 1:
            raise ValueError("time_risk_grace_loss_ratio must be between 0 and 1")
        self.time_risk_grace_ms = int(time_risk_grace_ms)
        self.time_risk_grace_loss_ratio = Decimal(str(time_risk_grace_loss_ratio))
        for label, value in (
            ("strong_strict_age_ms", strong_strict_age_ms),
            ("weak_strict_age_ms", weak_strict_age_ms),
        ):
            if value is not None:
                value = int(value)
                if value <= 0:
                    raise ValueError(f"{label} must be positive")
            setattr(self, label, value)
        for label, value in (
            ("profit_unlock_ratio", profit_unlock_ratio),
            ("profit_drawdown_ratio", profit_drawdown_ratio),
            ("profit_drawdown_peak_ratio", profit_drawdown_peak_ratio),
        ):
            if value is not None:
                value = Decimal(str(value))
                if not Decimal("0") < value < Decimal("1"):
                    raise ValueError(f"{label} must be between 0 and 1")
            setattr(self, label, value)
        if entry_premium_mult < 0:
            raise ValueError("entry_premium_mult must not be negative")
        self.entry_premium_mult = float(entry_premium_mult)
        self.entry_premium_floor = float(entry_premium_floor)
        self.entry_premium_cap = float(entry_premium_cap)
        if not 0 <= self.entry_premium_floor < self.entry_premium_cap:
            raise ValueError(
                "entry_premium_floor must be non-negative and below entry_premium_cap"
            )
        if entry_premium_base_pct < 0:
            raise ValueError("entry_premium_base_pct must not be negative")
        self.entry_premium_base_pct = float(entry_premium_base_pct)
        self.entry_scoring_enabled = bool(entry_scoring_enabled)
        self.entry_scoring_threshold = float(entry_scoring_threshold)
        if not 0 <= self.entry_scoring_threshold <= 1:
            raise ValueError("entry_scoring_threshold must be between 0 and 1")
        self.oi_stop_enabled = bool(oi_stop_enabled)
        self.oi_stop_oi_rise_pct = float(oi_stop_oi_rise_pct)
        self.oi_stop_loss_pct = float(oi_stop_loss_pct)
        if self.oi_stop_oi_rise_pct < 0:
            raise ValueError("oi_stop_oi_rise_pct must not be negative")
        if self.oi_stop_loss_pct < 0:
            raise ValueError("oi_stop_loss_pct must not be negative")
        self._oi_stop_checked = False
        self._oi_stop_campaign: int | None = None
        self._scoring_config: dict | None = None
        if entry_scoring_config:
            import json as _json

            self._scoring_config = _json.loads(
                Path(entry_scoring_config).read_text(encoding="utf-8")
            )
        self._premium_model: dict[str, object] | None = None
        if self.entry_premium_mult > 0:
            if entry_premium_model:
                import json as _json

                self._premium_model = _json.loads(
                    Path(entry_premium_model).read_text(encoding="utf-8")
                )
            else:
                from trading_platform.research.up_premium_model import (
                    COEFS,
                    FEATURES,
                    INTERCEPT,
                    MEAN,
                    STD,
                )

                self._premium_model = {
                    "features": list(FEATURES),
                    "mean": {f: float(v) for f, v in zip(FEATURES, MEAN)},
                    "std": {f: float(v) for f, v in zip(FEATURES, STD)},
                    "coefs": {f: float(v) for f, v in zip(FEATURES, COEFS)},
                    "intercept": float(INTERCEPT),
                }
        self.metrics_series = list(metrics_series or [])
        self._metrics_idx = 0

    def _group_bucket(
        self, rise_from_12h_low: Decimal | None
    ) -> tuple[bool, str]:
        grouped = self.group_rise_12h_threshold > 0
        if not grouped:
            return False, "default"
        strong = (
            rise_from_12h_low is not None
            and float(rise_from_12h_low) >= self.group_rise_12h_threshold
        )
        return True, "strong" if strong else "weak"

    def _entry_tier_atr_shift(
        self, rise_from_12h_low: Decimal | None
    ) -> Decimal:
        if self.strong_tier_atr_shift <= 0:
            return Decimal("0")
        grouped, bucket = self._group_bucket(rise_from_12h_low)
        if grouped and bucket == "strong":
            return Decimal(str(self.strong_tier_atr_shift))
        return Decimal("0")

    def _entry_scored_decision(
        self,
        *,
        spike_high: Decimal,
        atr: Decimal,
        tier_atr_shift: Decimal,
        bar: Bar1s,
        origin_price: Decimal,
        minute_start: int,
        rise_from_12h_low: Decimal | None,
    ) -> tuple[str, float, float] | None:
        """评分准入决策：低于阈值拒绝信号。

        返回 (rejection_reason, score, threshold) 表示拒绝；None 表示通过。
        评分未启用时恒返回 None。
        """
        if not self.entry_scoring_enabled or self._scoring_config is None:
            return None
        feats = self._entry_scoring_features(bar, origin_price)
        if feats is None:
            return None
        score = compute_score(feats, self._scoring_config)
        threshold = self.entry_scoring_threshold
        if score < threshold:
            return ("entry_scoring_threshold", score, threshold)
        return None

    def _entry_tier_prices(
        self,
        *,
        spike_high: Decimal,
        atr: Decimal,
        tier_atr_shift: Decimal,
        bar: Bar1s,
        origin_price: Decimal,
        minute_start: int,
        rise_from_12h_low: Decimal | None,
        scored: tuple[str, float, float] | None = None,
    ) -> list[Decimal] | None:
        """动态溢价单档挂单：触发价 × (1 + 溢价)。

        溢价 = base + S × 模型预测冲高% × mult（S 为评分）。
        entry_premium_mult=0 时关闭，回退默认 spike_high−ATR 三档。
        特征不足或模型缺失时同样回退默认三档。
        """
        if self.entry_premium_mult <= 0 or self._premium_model is None:
            return None
        feats = self._entry_scoring_features(bar, origin_price)
        if feats is None:
            return None
        model = self._premium_model
        features = model["features"]
        mean = model["mean"]
        std = model["std"]
        coefs = model["coefs"]
        pred = float(model["intercept"])
        for name in features:
            x = feats.get(name)
            if x is None or not np.isfinite(x):
                return None
            pred += coefs[name] * (x - mean[name]) / std[name]
        score = 1.0
        if self._scoring_config is not None:
            score = compute_score(feats, self._scoring_config)
        premium_pct = premium_pct_value(
            score,
            predicted_up_pct=pred,
            mult=self.entry_premium_mult,
            base_pct=self.entry_premium_base_pct,
            cap_pct=self.entry_premium_cap,
        )
        limit = bar.close * (Decimal("1") + Decimal(str(premium_pct)) / Decimal("100"))
        return [limit, limit, limit]

    def _entry_scoring_features(
        self, bar: Bar1s, origin_price: Decimal
    ) -> dict[str, float] | None:
        """触发时点评分特征（与 research_premium 同一口径，实时可算）。"""
        from trading_platform.strategies.spike.research_premium import (
            compute_trigger_features,
        )

        return compute_trigger_features(self, bar, origin_price)

    def _entry_bucket(self, rise_from_12h_low: Decimal | None) -> str | None:
        grouped, bucket = self._group_bucket(rise_from_12h_low)
        return bucket if grouped else None

    def _entry_filters_pass(self, event_ms: int) -> bool:
        return self._entry_filter_decision(event_ms)[0]

    def _entry_filter_decision(
        self,
        event_ms: int,
        rise_from_12h_low: Decimal | None = None,
    ) -> tuple[bool, dict[str, object] | None]:
        rejections: list[dict[str, object]] = []
        consecutive_rejection = self._consecutive_up_rejection_details(
            rise_from_12h_low
        )
        if consecutive_rejection is not None:
            rejections.append(consecutive_rejection)
        # 同一候选只读取一次指标快照，审计复用该决策以免推进游标两次。
        metrics_rejection = self._metrics_rejection_details(
            event_ms, rise_from_12h_low
        )
        if metrics_rejection is not None:
            rejections.append(metrics_rejection)
        top_maturity_rejection = self._top_maturity_rejection_details()
        if top_maturity_rejection is not None:
            rejections.append(top_maturity_rejection)
        if not rejections:
            return True, None
        if len(rejections) == 1:
            return False, rejections[0]
        details: dict[str, object] = {
            "rejection_stage": "combined_entry_filters",
            "rejection_reasons": [
                reason
                for rejection in rejections
                for reason in rejection["rejection_reasons"]
            ],
        }
        for rejection in rejections:
            details.update(
                {
                    key: value
                    for key, value in rejection.items()
                    if key not in {"rejection_stage", "rejection_reasons"}
                }
            )
        return False, details

    def _consecutive_up_rejection_details(
        self, rise_from_12h_low: Decimal | None
    ) -> dict[str, object] | None:
        """按动能分组选择连阳上限：强势桶用严格上限，弱势/蓄力桶用宽松上限。

        ``group_rise_12h_threshold`` 为 0 时分组关闭，行为与单一
        ``max_consecutive_up_minutes`` 完全一致。
        """
        grouped, bucket = self._group_bucket(rise_from_12h_low)
        if not grouped and self.max_consecutive_up_minutes <= 0:
            return None
        if grouped and self.max_consecutive_up_minutes <= 0 and self.loose_consecutive_up_minutes <= 0:
            return None
        effective_max = self.max_consecutive_up_minutes
        if grouped:
            effective_max = (
                self.max_consecutive_up_minutes
                if bucket == "strong"
                else self.loose_consecutive_up_minutes
            )
        if effective_max <= 0:
            return None
        consecutive_up_minutes = self._consecutive_up_minutes()
        if consecutive_up_minutes <= effective_max:
            return None
        details: dict[str, object] = {
            "rejection_stage": "consecutive_up_entry_filter",
            "rejection_reasons": ["max_consecutive_up_minutes"],
            "consecutive_up_minutes": consecutive_up_minutes,
            "max_consecutive_up_minutes": effective_max,
        }
        if grouped:
            details.update({
                "bucket": bucket,
                "rise_from_12h_low": (
                    str(rise_from_12h_low)
                    if rise_from_12h_low is not None
                    else None
                ),
                "group_rise_12h_threshold": self.group_rise_12h_threshold,
            })
        return details

    def _top_maturity_rejection_details(self) -> dict[str, object] | None:
        if (
            self.min_td_sell_setup_5m <= 0
            and self.min_volume_multiple_5m <= 0
        ):
            return None
        context = entry_context_features(self.klines_5m, self.klines_15m)
        rejection_reasons = [
            reason
            for reason, rejected in (
                (
                    "min_td_sell_setup_5m",
                    self.min_td_sell_setup_5m > 0
                    and (
                        context.td_sell_setup_5m is None
                        or context.td_sell_setup_5m < self.min_td_sell_setup_5m
                    ),
                ),
                (
                    "min_volume_multiple_5m",
                    self.min_volume_multiple_5m > 0
                    and (
                        context.volume_multiple_5m is None
                        or context.volume_multiple_5m
                        < self.min_volume_multiple_5m
                    ),
                ),
            )
            if rejected
        ]
        if not rejection_reasons:
            return None
        return {
            "rejection_stage": "top_maturity_entry_filter",
            "rejection_reasons": rejection_reasons,
            "td_sell_setup_5m": context.td_sell_setup_5m,
            "min_td_sell_setup_5m": self.min_td_sell_setup_5m,
            "volume_multiple_5m": (
                str(context.volume_multiple_5m)
                if context.volume_multiple_5m is not None
                else None
            ),
            "min_volume_multiple_5m": str(self.min_volume_multiple_5m),
        }

    def _consecutive_up_minutes(self) -> int:
        count = 0
        for kline in reversed(self.klines_1m):
            if kline.close <= kline.open:
                break
            count += 1
        return count

    def _metrics_snapshot_at(
        self, event_ms: int
    ) -> tuple[float, float, float] | None:
        snapshot = self._metrics_snapshot_with_available_time(event_ms)
        if snapshot is None:
            return None
        _, oi, previous_oi, long_short_ratio = snapshot
        return oi, previous_oi, long_short_ratio

    def _metrics_snapshot_with_available_time(
        self, event_ms: int
    ) -> tuple[int, float, float, float] | None:
        if not self.metrics_series:
            return None
        while (
            self._metrics_idx < len(self.metrics_series)
            and self.metrics_series[self._metrics_idx][0] <= event_ms
        ):
            self._metrics_idx += 1
        idx = self._metrics_idx - 1
        if idx < 0:
            return None
        current = self.metrics_series[idx]
        previous_oi = self.metrics_series[idx - 1][1] if idx >= 1 else current[1]
        return current[0], current[1], previous_oi, current[2]

    def _metrics_blocked(self, event_ms: int) -> bool:
        return self._metrics_rejection_details(event_ms) is not None

    def _oi_stop_decision(self, event_ms: int, mark_price: Decimal) -> list:
        """OI 止损：插针后首个有效 5m OI 点相对基准点升幅超阈值且浮亏达标。

        时间对齐（5m 粒度）：
        - 基准点 = signal_time 前最近 available_ms 的 OI 点（如 30:00）
        - 确认点 = signal_time 后第一个 available_ms 的 OI 点（如 35:00）
        - 仅当 event_ms >= 确认点 available_ms（数据可见）时评估一次。
        """
        if (
            not self.oi_stop_enabled
            or self.first_fill_time is None
            or self._account is None
            or not self.metrics_series
        ):
            return []
        position = self._account.get_position(self.symbol)
        if position is None or position.side != "SHORT" or position.quantity <= 0:
            return []
        campaign_id = self._campaign_id_for_timing or ""
        prefix = f"spike_short:{self.symbol}:"
        if not campaign_id.startswith(prefix):
            return []
        try:
            signal_time = int(campaign_id[len(prefix):])
        except ValueError:
            return []
        if self._oi_stop_campaign != signal_time:
            self._oi_stop_campaign = signal_time
            self._oi_stop_checked = False
        if self._oi_stop_checked:
            return []
        base_oi = None
        confirm = None
        for ms, oi, _ls in self.metrics_series:
            if ms <= signal_time:
                base_oi = (ms, float(oi))
            elif confirm is None and oi is not None:
                confirm = (ms, float(oi))
                break
        if base_oi is None or confirm is None or base_oi[1] <= 0:
            return []
        if event_ms < confirm[0]:
            return []
        self._oi_stop_checked = True
        d_oi = (confirm[1] - base_oi[1]) / base_oi[1] * 100.0
        if d_oi <= self.oi_stop_oi_rise_pct:
            return []
        entry = float(position.entry_price)
        if entry <= 0:
            return []
        loss_pct = (float(mark_price) - entry) / entry * 100.0
        if loss_pct < self.oi_stop_loss_pct:
            return []
        self._exit_requested = True
        self._record_audit(
            event_time=event_ms,
            event_type="candidate_oi_stop_exit_requested",
            campaign_id=campaign_id,
            details={
                "base_ms": base_oi[0],
                "confirm_ms": confirm[0],
                "d_oi_pct": round(d_oi, 3),
                "loss_pct": round(loss_pct, 3),
                "mark_price": str(mark_price),
            },
        )
        return [
            OrderIntent(
                symbol=self.symbol,
                side="BUY",
                price=mark_price,
                quantity=position.quantity,
                client_order_id=build_exit_client_order_id(
                    self.symbol, event_ms, "c"
                ),
                order_type="MARKET",
                reduce_only=True,
                strategy_id="spike_short",
                trigger_reason="candidate_oi_stop_exit",
            )
        ]

    def _manage_candidate_exit(
        self, event_time: int, mark_price: Decimal
    ) -> list[OrderIntent]:
        """先评估 OI 止损（优先级最高），命中则直接退出。"""
        oi_stop_intent = self._oi_stop_decision(event_time, mark_price)
        if oi_stop_intent:
            return oi_stop_intent
        return super()._manage_candidate_exit(event_time, mark_price)

    def _metrics_rejection_details(
        self, event_ms: int, rise_from_12h_low: Decimal | None = None
    ) -> dict[str, object] | None:
        if self.max_oi_change_pct <= 0 and self.max_ls_ratio <= 0:
            return None
        grouped, bucket = self._group_bucket(rise_from_12h_low)
        effective_ls_ratio = self.max_ls_ratio
        if (
            grouped
            and bucket == "weak"
            and self.loose_max_ls_ratio is not None
        ):
            effective_ls_ratio = self.loose_max_ls_ratio
        if self.max_oi_change_pct <= 0 and effective_ls_ratio <= 0:
            return None
        snapshot = self._metrics_snapshot_with_available_time(event_ms)
        if snapshot is None:
            return None
        metrics_available_time, oi, previous_oi, long_short_ratio = snapshot
        oi_change = (oi - previous_oi) / previous_oi * 100 if previous_oi else 0.0
        rejection_reasons = [
            reason
            for reason, rejected in (
                (
                    "max_oi_change_pct",
                    self.max_oi_change_pct > 0
                    and oi_change > self.max_oi_change_pct,
                ),
                (
                    "max_ls_ratio",
                    effective_ls_ratio > 0
                    and long_short_ratio > effective_ls_ratio,
                ),
            )
            if rejected
        ]
        if not rejection_reasons:
            return None
        details: dict[str, object] = {
            "rejection_stage": "metrics_entry_filters",
            "rejection_reasons": rejection_reasons,
            "oi": oi,
            "previous_oi": previous_oi,
            "oi_change_pct": oi_change,
            "ls_ratio": long_short_ratio,
            "metrics_available_time": metrics_available_time,
            "max_oi_change_pct": self.max_oi_change_pct,
            "max_ls_ratio": effective_ls_ratio,
        }
        if grouped and bucket == "weak" and self.loose_max_ls_ratio is not None:
            details.update({
                "bucket": bucket,
                "rise_from_12h_low": (
                    str(rise_from_12h_low)
                    if rise_from_12h_low is not None
                    else None
                ),
                "group_rise_12h_threshold": self.group_rise_12h_threshold,
                "loose_max_ls_ratio": self.loose_max_ls_ratio,
            })
        return details


class V21:
    name = "v2.1"
    strategy_class = SpikeV21Strategy
    data_requirements = SpikeDataRequirements(metrics_5m=True)
    defaults = SpikeStrategyDefaults(
        exit_policy=V2.defaults.exit_policy,
        prior_high_lookback_hours=V2.defaults.prior_high_lookback_hours,
        rise_low_lookback_hours=V2.defaults.rise_low_lookback_hours,
        min_rise_duration_hours=V2.defaults.min_rise_duration_hours,
        entry_tier_mode=V2.defaults.entry_tier_mode,
        profit_unlock_percent=3.0,
    )
    supported_parameters = frozenset(
        {
            "reject_below_current",
            "box_duration_min_minutes",
            "spike_avg_deviation_max_pct",
            "spike_range_max_pct",
            "spike_vwap_deviation_max_pct",
            "max_consecutive_up_minutes",
            "max_oi_change_pct",
            "max_ls_ratio",
            "rise_5s_threshold",
            "accel_rise_5s_min",
            "accel_ratio",
            "accel_prev_minutes",
            "max_rise_5s_percent",
            "max_rise_window_seconds",
            "max_rise_window_percent",
            "max_volume_multiple_5s",
            "min_td_sell_setup_5m",
            "min_volume_multiple_5m",
            "group_rise_12h_threshold",
            "loose_consecutive_up_minutes",
            "loose_max_ls_ratio",
            "strong_tier_atr_shift",
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
            "entry_premium_mult",
            "entry_premium_floor",
            "entry_premium_cap",
            "entry_premium_model",
            "entry_scoring_enabled",
            "entry_scoring_threshold",
            "entry_scoring_config",
            "entry_premium_base_pct",
            "oi_stop_enabled",
            "oi_stop_oi_rise_pct",
            "oi_stop_loss_pct",
        }
    )
    internal_parameters = frozenset({"metrics_series"})
