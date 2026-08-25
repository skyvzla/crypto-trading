"""Spike v2 冻结策略声明（不包含后续指标过滤研究）。"""

from trading_platform.strategies.spike.definition import (
    SPIKE_V2_SHARED_METRICS,
    SPIKE_V2_SHARED_FEATURES,
    SpikeDataRequirements,
    SpikeStrategyDefaults,
)
from trading_platform.strategies.spike.shared_features import (
    SpikeSharedFeatureProvider,
)
from trading_platform.strategies.spike.short import DynamicSpikeShortStrategy


class SpikeV2Strategy(DynamicSpikeShortStrategy):
    strategy_name = "v2"


class V2:
    name = "v2"
    strategy_class = SpikeV2Strategy
    shared_feature_provider = SpikeSharedFeatureProvider
    data_requirements = SpikeDataRequirements(
        metrics_5m=False,
        bar1s_feature_columns=frozenset(),
        shared_features=SPIKE_V2_SHARED_FEATURES,
        shared_metrics=SPIKE_V2_SHARED_METRICS,
    )
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
