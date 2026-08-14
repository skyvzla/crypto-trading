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
        if (
            self.max_consecutive_up_minutes > 0
            and self._consecutive_up_minutes() > self.max_consecutive_up_minutes
        ):
            return False
        return not self._metrics_blocked(event_ms)

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
        return current[1], previous_oi, current[2]

    def _metrics_blocked(self, event_ms: int) -> bool:
        if self.max_oi_change_pct <= 0 and self.max_ls_ratio <= 0:
            return False
        snapshot = self._metrics_snapshot_at(event_ms)
        if snapshot is None:
            return False
        oi, previous_oi, long_short_ratio = snapshot
        if self.max_ls_ratio > 0 and long_short_ratio > self.max_ls_ratio:
            return True
        oi_change = (oi - previous_oi) / previous_oi * 100 if previous_oi else 0.0
        return self.max_oi_change_pct > 0 and oi_change > self.max_oi_change_pct


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
