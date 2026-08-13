"""Spike 策略实现的声明与动态加载。"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from typing import Any, Protocol

from trading_platform.backtest.strategy_definition import MarketDataRequirements


@dataclass(frozen=True)
class SpikeDataRequirements(MarketDataRequirements):
    """Spike 在通用行情需求之外使用的指标数据。"""

    market_timeframes: tuple[str, ...] = ("1s", "1m", "5m", "15m")
    execution_timeframe: str = "1s"
    metrics_5m: bool = False


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
            "metrics_5m": definition.data_requirements.metrics_5m,
        },
    }
