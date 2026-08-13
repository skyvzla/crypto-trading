"""Spike v1 冻结策略声明。"""

from trading_platform.strategies.spike.definition import (
    SpikeDataRequirements,
    SpikeStrategyDefaults,
)
from trading_platform.strategies.spike.short import DynamicSpikeShortStrategy


class SpikeV1Strategy(DynamicSpikeShortStrategy):
    strategy_name = "v1"


class V1:
    name = "v1"
    strategy_class = SpikeV1Strategy
    data_requirements = SpikeDataRequirements(
        market_timeframes=("1s", "1m", "5m"), metrics_5m=False
    )
    defaults = SpikeStrategyDefaults(
        exit_policy="confirmed",
        prior_high_lookback_hours=4,
        rise_low_lookback_hours=0,
        min_rise_duration_hours=0,
        entry_tier_mode="three-tier",
        profit_unlock_percent=None,
    )
    supported_parameters = frozenset()
    internal_parameters = frozenset()
