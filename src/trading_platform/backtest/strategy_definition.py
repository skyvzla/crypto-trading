"""回测策略装配与数据订阅的通用契约。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MarketDataRequirements:
    """完整策略运行所需的行情，以及用于撮合的时间粒度。"""

    market_timeframes: tuple[str, ...]
    execution_timeframe: str

    def __post_init__(self) -> None:
        if not self.market_timeframes:
            raise ValueError("market_timeframes must not be empty")
        if self.execution_timeframe not in self.market_timeframes:
            raise ValueError("execution_timeframe must be included in market_timeframes")
