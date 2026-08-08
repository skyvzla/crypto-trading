from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True)
class Candle:
    symbol: str
    timeframe: str
    open_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time: datetime

    def __post_init__(self) -> None:
        if self.open_time.tzinfo is None or self.close_time.tzinfo is None:
            raise ValueError("candle timestamps must include a timezone")
        if self.close_time <= self.open_time:
            raise ValueError("close_time must be later than open_time")

    @property
    def normalized_symbol(self) -> str:
        return self.symbol.strip().upper()

    @property
    def open_time_utc(self) -> datetime:
        return self.open_time.astimezone(UTC)

    @property
    def close_time_utc(self) -> datetime:
        return self.close_time.astimezone(UTC)
