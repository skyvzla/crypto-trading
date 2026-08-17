"""Spike v2.2 策略声明：v2.1 全参数加完整退出候选基线。

D-027 候选合并：深回撤保护（profit_drawdown_peak_ratio=0.2 /
profit_drawdown_ratio=0.1，1m 粒度）与静态强弱分桶
（strong_bucket_strict_age_ms=1500000 / weak_bucket_strict_age_ms=600000）
经 92 币全量验证为候选最优组合；v2.2 声明保持与 v2.1 相同的
supported_parameters，默认 profit_unlock_percent 对齐全量验证基线
（1.5%），候选退出参数不写入默认值、由配置显式传入。
"""

from __future__ import annotations

from decimal import Decimal

from trading_platform.strategies.spike.definition import (
    SpikeDataRequirements,
    SpikeStrategyDefaults,
)
from trading_platform.strategies.spike.v2_1 import SpikeV21Strategy, V21


class V22:
    name = "v2.2"
    strategy_class = SpikeV21Strategy
    data_requirements = V21.data_requirements
    defaults = SpikeStrategyDefaults(
        exit_policy="candidate-v1",
        prior_high_lookback_hours=V21.defaults.prior_high_lookback_hours,
        rise_low_lookback_hours=V21.defaults.rise_low_lookback_hours,
        min_rise_duration_hours=V21.defaults.min_rise_duration_hours,
        entry_tier_mode=V21.defaults.entry_tier_mode,
        profit_unlock_percent=Decimal("1.5"),
    )
    supported_parameters = V21.supported_parameters
    internal_parameters = V21.internal_parameters