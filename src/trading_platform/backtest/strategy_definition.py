"""回测策略装配与数据订阅的通用契约。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class FeatureSpec:
    """策略声明的可共享行情特征。"""

    name: str
    timeframe: str


@dataclass(frozen=True)
class MarketDataRequirements:
    """完整策略运行所需的行情，以及用于撮合的时间粒度。"""

    market_timeframes: tuple[str, ...]
    execution_timeframe: str
    shared_features: frozenset[FeatureSpec] = frozenset()
    bar1s_feature_columns: frozenset[str] | None = None

    def __post_init__(self) -> None:
        if not self.market_timeframes:
            raise ValueError("market_timeframes must not be empty")
        if self.execution_timeframe not in self.market_timeframes:
            raise ValueError("execution_timeframe must be included in market_timeframes")
        shared_features = frozenset(self.shared_features)
        object.__setattr__(self, "shared_features", shared_features)
        if self.bar1s_feature_columns is not None:
            object.__setattr__(
                self,
                "bar1s_feature_columns",
                frozenset(self.bar1s_feature_columns),
            )
        invalid_timeframes = {
            feature.timeframe
            for feature in shared_features
            if feature.timeframe not in self.market_timeframes
        }
        if invalid_timeframes:
            names = ", ".join(sorted(invalid_timeframes))
            raise ValueError(
                "shared feature timeframes must be included in market_timeframes: "
                f"{names}"
            )


class SharedFeatureProvider(Protocol):
    """参数 sweep 可选的共享特征提供器接口。"""

    def bind(self, consumer: object) -> None: ...

    def process_event(self, event: object) -> None: ...
