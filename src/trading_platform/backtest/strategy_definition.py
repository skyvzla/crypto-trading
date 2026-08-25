"""回测策略装配与数据订阅的通用契约。"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, Protocol, Sequence, TypeGuard

from trading_platform.shared.events import Kline


type SharedMetricScalar = None | bool | int | float | str | bytes | Decimal
type SharedMetricResult = SharedMetricScalar | tuple[SharedMetricResult, ...]

_SHARED_METRIC_SCALAR_TYPES = frozenset({
    type(None),
    bool,
    int,
    float,
    str,
    bytes,
    Decimal,
})


def is_shared_metric_result(value: object) -> TypeGuard[SharedMetricResult]:
    """只接受项目明确支持的递归不可变共享结果。"""
    if type(value) in _SHARED_METRIC_SCALAR_TYPES:
        return True
    return type(value) is tuple and all(
        is_shared_metric_result(item) for item in value
    )


@dataclass(frozen=True)
class FeatureSpec:
    """策略声明的可共享行情特征。"""

    name: str
    timeframe: str


@dataclass(frozen=True)
class SharedMetricSpec:
    """策略可声明的纯行情窗口计算。

    ``compute`` 只能依赖传入的窗口，并返回 ``SharedMetricResult``。窗口
    容器不可变，但兼容既有事件模型，其中的 ``Kline`` 自身仍然可变；
    compute 必须把事件视为只读，provider 不为每次计算做昂贵的深拷贝。
    策略私有状态不进入该接口。
    """

    feature: FeatureSpec
    compute: Callable[[Sequence[Kline]], SharedMetricResult] = field(repr=False)
    retention_minutes: int | Callable[[Any], int]

    def resolve_retention_minutes(self, settings: Any = None) -> int:
        """解析该指标所需的行情保留时长；解析失败必须显式暴露。"""
        retention = self.retention_minutes
        value = retention(settings) if callable(retention) else retention
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(
                "shared metric retention_minutes must resolve to a positive integer: "
                f"{self.feature.name}@{self.feature.timeframe}"
            )
        return value


def validate_shared_metric_timeframes(
    metrics: Iterable[SharedMetricSpec],
) -> tuple[SharedMetricSpec, ...]:
    """校验当前共享窗口实现支持的指标粒度。"""
    metric_specs = tuple(metrics)
    unsupported = sorted(
        f"{metric.feature.name}@{metric.feature.timeframe}"
        for metric in metric_specs
        if metric.feature.timeframe != "1m"
    )
    if unsupported:
        raise ValueError(
            "shared metrics currently support only the 1m timeframe: "
            + ", ".join(unsupported)
        )
    return metric_specs


def normalize_shared_metric_specs(
    metrics: Iterable[SharedMetricSpec],
) -> tuple[SharedMetricSpec, ...]:
    """按稳定 feature key 去重，并拒绝同 key 的冲突定义。"""
    metric_by_feature: dict[FeatureSpec, SharedMetricSpec] = {}
    for metric in validate_shared_metric_timeframes(metrics):
        existing = metric_by_feature.get(metric.feature)
        if existing is not None and existing != metric:
            raise ValueError(
                "conflicting shared metric definitions: "
                f"{metric.feature.name}@{metric.feature.timeframe}"
            )
        if existing is None:
            metric_by_feature[metric.feature] = metric
    return tuple(metric_by_feature.values())


def aggregate_shared_metric_retention(
    metric_consumers: Iterable[tuple[SharedMetricSpec, Any]],
) -> dict[FeatureSpec, int]:
    """按每个指标及其所属消费者 settings 线性聚合 retention。"""
    consumers = tuple(metric_consumers)
    normalize_shared_metric_specs(metric for metric, _settings in consumers)
    retention: dict[FeatureSpec, int] = {}
    for metric, settings in consumers:
        value = metric.resolve_retention_minutes(settings)
        retention[metric.feature] = max(retention.get(metric.feature, 0), value)
    return retention


@dataclass(frozen=True)
class MarketDataRequirements:
    """完整策略运行所需的行情，以及用于撮合的时间粒度。"""

    market_timeframes: tuple[str, ...]
    execution_timeframe: str
    shared_features: frozenset[FeatureSpec] = frozenset()
    bar1s_feature_columns: frozenset[str] | None = None
    shared_metrics: tuple[SharedMetricSpec, ...] = ()
    retention_minutes: int | Callable[[Any], int] = 30 * 60

    def __post_init__(self) -> None:
        if not self.market_timeframes:
            raise ValueError("market_timeframes must not be empty")
        if self.execution_timeframe not in self.market_timeframes:
            raise ValueError("execution_timeframe must be included in market_timeframes")
        shared_features = frozenset(self.shared_features)
        object.__setattr__(self, "shared_features", shared_features)
        object.__setattr__(self, "shared_metrics", tuple(self.shared_metrics))
        if callable(self.retention_minutes):
            return_value = None
        else:
            return_value = self.retention_minutes
        if (
            return_value is not None
            and (
                isinstance(return_value, bool)
                or not isinstance(return_value, int)
                or return_value <= 0
            )
        ):
            raise ValueError("retention_minutes must be a positive integer or resolver")
        if self.bar1s_feature_columns is not None:
            object.__setattr__(
                self,
                "bar1s_feature_columns",
                frozenset(self.bar1s_feature_columns),
            )
        invalid_timeframes = {
            feature.timeframe
            for feature in (
                *shared_features,
                *(metric.feature for metric in self.shared_metrics),
            )
            if feature.timeframe not in self.market_timeframes
        }
        if invalid_timeframes:
            names = ", ".join(sorted(invalid_timeframes))
            raise ValueError(
                "shared feature timeframes must be included in market_timeframes: "
                f"{names}"
            )

    def resolve_retention_minutes(self, settings: Any = None) -> int:
        value = (
            self.retention_minutes(settings)
            if callable(self.retention_minutes)
            else self.retention_minutes
        )
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError("retention_minutes must resolve to a positive integer")
        return value


class SharedFeatureProvider(Protocol):
    """参数 sweep 可选的共享特征提供器接口。"""

    def bind(self, consumer: object) -> None: ...

    def process_event(self, event: object) -> None: ...


class SharedMetricProvider(SharedFeatureProvider, Protocol):
    """可选的、按稳定窗口 key 复用纯行情指标的 provider 扩展。"""

    def supports_metric(self, feature: FeatureSpec) -> bool: ...

    def window_metric(
        self, feature: FeatureSpec, window_end: int, periods: int
    ) -> SharedMetricResult: ...
