"""Spike v2.1 策略声明：v2 基线加连阳、OI 和多空比研究能力。"""

from __future__ import annotations

from trading_platform.strategies.spike.definition import (
    SpikeDataRequirements,
    SpikeStrategyDefaults,
)
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
        metrics_series: list[tuple[int, float, float]] | None = None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        if max_consecutive_up_minutes < 0:
            raise ValueError("max_consecutive_up_minutes must not be negative")
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
        {"max_consecutive_up_minutes", "max_oi_change_pct", "max_ls_ratio"}
    )
    internal_parameters = frozenset({"metrics_series"})
