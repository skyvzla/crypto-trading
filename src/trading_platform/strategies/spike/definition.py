"""Spike 策略实现的声明与动态加载。"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from typing import Any, Callable, Protocol, Sequence

from trading_platform.backtest.strategy_definition import (
    FeatureSpec,
    MarketDataRequirements,
    SharedMetricSpec,
)
from trading_platform.shared.events import Kline


SPIKE_RISE_5S_FEATURE = FeatureSpec(name="rise_5s", timeframe="1s")
SPIKE_RISE_60S_FEATURE = FeatureSpec(name="rise_60s", timeframe="1s")
SPIKE_ORDERFLOW_FEATURE = FeatureSpec(name="orderflow", timeframe="1s")
SPIKE_CANDIDATE_EXIT_FEATURE = FeatureSpec(
    name="candidate_exit", timeframe="1m"
)
SPIKE_MIN_LOW_1M_FEATURE = FeatureSpec(name="min_low_1m", timeframe="1m")
SPIKE_PRIOR_HIGH_1M_FEATURE = FeatureSpec(name="prior_high_1m", timeframe="1m")
SPIKE_V2_SHARED_FEATURES = frozenset({
    SPIKE_RISE_5S_FEATURE,
    SPIKE_CANDIDATE_EXIT_FEATURE,
    SPIKE_MIN_LOW_1M_FEATURE,
    SPIKE_PRIOR_HIGH_1M_FEATURE,
})


def spike_1m_retention_minutes(
    *,
    rise_low_lookback_minutes: int,
    prior_high_lookback_minutes: int,
    box_duration_min_minutes: int,
) -> int:
    """统一计算 Spike 策略需要保留的完整 1m 窗口。"""
    return max(
        30 * 60,
        int(rise_low_lookback_minutes),
        int(prior_high_lookback_minutes),
        7 * 24 * 60 if int(box_duration_min_minutes) > 0 else 0,
    )


def _resolve_spike_1m_retention(settings: Any) -> int:
    return spike_1m_retention_minutes(
        rise_low_lookback_minutes=settings.rise_low_lookback_minutes,
        prior_high_lookback_minutes=settings.prior_high_lookback_minutes,
        box_duration_min_minutes=settings.box_duration_min_minutes,
    )


def _min_low_retention(settings: Any) -> int:
    return max(16 * 60, int(settings.rise_low_lookback_minutes))


def _prior_high_retention(settings: Any) -> int:
    return max(1, int(settings.prior_high_lookback_minutes))


def _min_low_1m(window: Sequence[Kline]) -> tuple[Any, int] | None:
    if not window:
        return None
    point = min(window, key=lambda item: (item.low, -item.open_time))
    return point.low, point.open_time


def _prior_high_1m(window: Sequence[Kline]) -> tuple[Any, int] | None:
    if not window:
        return None
    point = max(window, key=lambda item: (item.high, item.open_time))
    return point.high, point.open_time


SPIKE_V2_SHARED_METRICS = (
    SharedMetricSpec(
        SPIKE_MIN_LOW_1M_FEATURE,
        _min_low_1m,
        retention_minutes=_min_low_retention,
    ),
    SharedMetricSpec(
        SPIKE_PRIOR_HIGH_1M_FEATURE,
        _prior_high_1m,
        retention_minutes=_prior_high_retention,
    ),
)


@dataclass(frozen=True)
class SpikeDataRequirements(MarketDataRequirements):
    """Spike 在通用行情需求之外使用的指标数据。"""

    market_timeframes: tuple[str, ...] = ("1s", "1m", "5m", "15m")
    execution_timeframe: str = "1s"
    shared_features: frozenset[FeatureSpec] = frozenset()
    metrics_5m: bool = False
    shared_metrics: tuple[SharedMetricSpec, ...] = ()
    retention_minutes: int | Callable[[Any], int] = _resolve_spike_1m_retention


@dataclass(frozen=True)
class SpikeStrategyDefaults:
    """策略模块提供的默认参数；命令行显式参数仍可覆盖。"""

    exit_policy: str
    prior_high_lookback_hours: int
    rise_low_lookback_hours: int
    min_rise_duration_hours: int
    entry_tier_mode: str
    profit_unlock_percent: float | None


class SpikeStrategyDefinition(Protocol):
    name: str
    strategy_class: type
    data_requirements: SpikeDataRequirements
    shared_feature_provider: type | None
    defaults: SpikeStrategyDefaults
    supported_parameters: frozenset[str]
    internal_parameters: frozenset[str]


def load_strategy_definition(path: str) -> SpikeStrategyDefinition:
    """按 ``module:attribute`` 加载策略声明。"""

    module_name, separator, attribute = path.partition(":")
    if not separator or not module_name or not attribute:
        raise ValueError("strategy must use module:attribute format")
    try:
        definition = getattr(import_module(module_name), attribute)
    except (ImportError, AttributeError) as error:
        raise ValueError(f"cannot load strategy definition: {path}") from error
    for field in (
        "name",
        "strategy_class",
        "data_requirements",
        "shared_feature_provider",
        "defaults",
        "supported_parameters",
        "internal_parameters",
    ):
        if not hasattr(definition, field):
            raise ValueError(f"strategy definition {path} is missing {field}")
    return definition


def strategy_metadata(definition: SpikeStrategyDefinition, path: str) -> dict[str, Any]:
    return {
        "strategy": path,
        "strategy_name": definition.name,
        "data_requirements": {
            "market_timeframes": list(definition.data_requirements.market_timeframes),
            "execution_timeframe": definition.data_requirements.execution_timeframe,
            "shared_features": [
                {"name": feature.name, "timeframe": feature.timeframe}
                for feature in sorted(
                    definition.data_requirements.shared_features,
                    key=lambda feature: (feature.timeframe, feature.name),
                )
            ],
            "shared_metrics": [
                {
                    "name": metric.feature.name,
                    "timeframe": metric.feature.timeframe,
                }
                for metric in sorted(
                    definition.data_requirements.shared_metrics,
                    key=lambda metric: (
                        metric.feature.timeframe,
                        metric.feature.name,
                    ),
                )
            ],
            "metrics_5m": definition.data_requirements.metrics_5m,
        },
    }
