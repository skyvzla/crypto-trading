"""Spike v1.1：v1 基线加可选指标过滤和前高偏差研究参数。"""

from __future__ import annotations

from trading_platform.strategies.spike.definition import (
    SPIKE_CANDIDATE_EXIT_FEATURE,
    SPIKE_RISE_5S_FEATURE,
    SpikeDataRequirements,
    SpikeStrategyDefaults,
)
from trading_platform.strategies.spike.shared_features import (
    SpikeSharedFeatureProvider,
)
from trading_platform.strategies.spike.short import DynamicSpikeShortStrategy


class SpikeV11Strategy(DynamicSpikeShortStrategy):
    strategy_name = "v1.1"

    def __init__(
        self,
        *args,
        max_consecutive_up_minutes: int = 0,
        max_oi_change_pct: float = 0.0,
        max_ls_ratio: float = 0.0,
        metrics_series: list[tuple[int, float, float]] | None = None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        if max_consecutive_up_minutes < 0:
            raise ValueError("max_consecutive_up_minutes must not be negative")
        if max_oi_change_pct < 0 or max_ls_ratio < 0:
            raise ValueError("metric thresholds must not be negative")
        self.max_consecutive_up_minutes = int(max_consecutive_up_minutes)
        self.max_oi_change_pct = float(max_oi_change_pct)
        self.max_ls_ratio = float(max_ls_ratio)
        self.metrics_series = list(metrics_series or [])
        self._metrics_idx = 0

    def _entry_filters_pass(self, event_ms: int) -> bool:
        return self._entry_filter_decision(event_ms)[0]

    def _entry_filter_decision(
        self, event_ms: int
    ) -> tuple[bool, dict[str, object] | None]:
        consecutive_rejection = None
        if self.max_consecutive_up_minutes > 0:
            consecutive_up_minutes = self._consecutive_up_minutes()
            if consecutive_up_minutes > self.max_consecutive_up_minutes:
                consecutive_rejection = {
                    "rejection_stage": "consecutive_up_entry_filter",
                    "rejection_reasons": ["max_consecutive_up_minutes"],
                    "consecutive_up_minutes": consecutive_up_minutes,
                    "max_consecutive_up_minutes": self.max_consecutive_up_minutes,
                }
        # 同一候选只读取一次指标快照，审计复用该决策以免推进游标两次。
        metrics_rejection = self._metrics_rejection_details(event_ms)
        if consecutive_rejection is None:
            return metrics_rejection is None, metrics_rejection
        if metrics_rejection is None:
            return False, consecutive_rejection
        return False, {
            **metrics_rejection,
            "rejection_stage": "combined_entry_filters",
            "rejection_reasons": [
                *consecutive_rejection["rejection_reasons"],
                *metrics_rejection["rejection_reasons"],
            ],
            "consecutive_up_minutes": consecutive_rejection[
                "consecutive_up_minutes"
            ],
            "max_consecutive_up_minutes": consecutive_rejection[
                "max_consecutive_up_minutes"
            ],
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
        while (
            self._metrics_idx < len(self.metrics_series)
            and self.metrics_series[self._metrics_idx][0] <= event_ms
        ):
            self._metrics_idx += 1
        idx = self._metrics_idx - 1
        if idx < 0:
            return None
        current = self.metrics_series[idx]
        previous_oi = self.metrics_series[idx - 1][1] if idx else current[1]
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


class V11:
    name = "v1.1"
    strategy_class = SpikeV11Strategy
    data_requirements = SpikeDataRequirements(
        market_timeframes=("1s", "1m", "5m", "15m"),
        shared_features=frozenset({
            SPIKE_RISE_5S_FEATURE,
            SPIKE_CANDIDATE_EXIT_FEATURE,
        }),
        metrics_5m=True,
    )
    shared_feature_provider = SpikeSharedFeatureProvider
    defaults = SpikeStrategyDefaults(
        exit_policy="candidate-v1",
        prior_high_lookback_hours=4,
        rise_low_lookback_hours=0,
        min_rise_duration_hours=0,
        entry_tier_mode="three-tier",
        profit_unlock_percent=None,
    )
    supported_parameters = frozenset({
        "max_consecutive_up_minutes", "max_oi_change_pct", "max_ls_ratio",
        "rise_5s_threshold_percent", "rise_5s_threshold",
        "max_rise_5s_percent", "max_volume_multiple_5s",
        "prior_high_tolerance_percent",
    })
    internal_parameters = frozenset({"metrics_series"})
