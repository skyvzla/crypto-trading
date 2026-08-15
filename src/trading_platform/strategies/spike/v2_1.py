"""Spike v2.1 策略声明：v2 基线加连阳、OI 和多空比研究能力。"""

from __future__ import annotations

from decimal import Decimal

from trading_platform.strategies.spike.definition import (
    SpikeDataRequirements,
    SpikeStrategyDefaults,
)
from trading_platform.strategies.spike.entry_features import entry_context_features
from trading_platform.strategies.spike.v2 import V2
from trading_platform.strategies.spike.short import DynamicSpikeShortStrategy


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
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
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
        self.metrics_series = list(metrics_series or [])
        self._metrics_idx = 0

    def _entry_filters_pass(self, event_ms: int) -> bool:
        return self._entry_filter_decision(event_ms)[0]

    def _entry_filter_decision(
        self, event_ms: int
    ) -> tuple[bool, dict[str, object] | None]:
        rejections: list[dict[str, object]] = []
        if self.max_consecutive_up_minutes > 0:
            consecutive_up_minutes = self._consecutive_up_minutes()
            if consecutive_up_minutes > self.max_consecutive_up_minutes:
                rejections.append({
                    "rejection_stage": "consecutive_up_entry_filter",
                    "rejection_reasons": ["max_consecutive_up_minutes"],
                    "consecutive_up_minutes": consecutive_up_minutes,
                    "max_consecutive_up_minutes": self.max_consecutive_up_minutes,
                })
        # 同一候选只读取一次指标快照，审计复用该决策以免推进游标两次。
        metrics_rejection = self._metrics_rejection_details(event_ms)
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

    def _metrics_rejection_details(
        self, event_ms: int
    ) -> dict[str, object] | None:
        if self.max_oi_change_pct <= 0 and self.max_ls_ratio <= 0:
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
                    self.max_ls_ratio > 0 and long_short_ratio > self.max_ls_ratio,
                ),
            )
            if rejected
        ]
        if not rejection_reasons:
            return None
        return {
            "rejection_stage": "metrics_entry_filters",
            "rejection_reasons": rejection_reasons,
            "oi": oi,
            "previous_oi": previous_oi,
            "oi_change_pct": oi_change,
            "ls_ratio": long_short_ratio,
            "metrics_available_time": metrics_available_time,
            "max_oi_change_pct": self.max_oi_change_pct,
            "max_ls_ratio": self.max_ls_ratio,
        }


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
            "max_consecutive_up_minutes",
            "max_oi_change_pct",
            "max_ls_ratio",
            "max_rise_5s_percent",
            "max_volume_multiple_5s",
            "min_td_sell_setup_5m",
            "min_volume_multiple_5m",
        }
    )
    internal_parameters = frozenset({"metrics_series"})
