"""Spike v2 冻结策略声明（不包含后续指标过滤研究）。"""

from trading_platform.strategies.spike.definition import (
    SpikeDataRequirements,
    SpikeStrategyDefaults,
)
from trading_platform.strategies.spike.short import DynamicSpikeShortStrategy


class SpikeV2Strategy(DynamicSpikeShortStrategy):
    strategy_name = "v2"


class V2:
    name = "v2"
    strategy_class = SpikeV2Strategy
    data_requirements = SpikeDataRequirements(metrics_5m=False)
    defaults = SpikeStrategyDefaults(
        exit_policy="candidate-v1",
        prior_high_lookback_hours=6,
        rise_low_lookback_hours=7 * 24,
        min_rise_duration_hours=24,
        entry_tier_mode="tier3-only",
        profit_unlock_percent=1.5,
    )
    supported_parameters = frozenset()
    internal_parameters = frozenset()
