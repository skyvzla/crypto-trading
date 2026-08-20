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


@dataclass(frozen=True)
class Candle1s(Candle):
    """由 aggTrade 聚合得到的 1s K 线及其无损订单流统计。"""

    vwap: float | None = None
    quote_volume: float | None = None
    trade_count: int | None = None
    raw_trade_count: int | None = None
    taker_buy_volume: float | None = None
    taker_sell_volume: float | None = None
    taker_buy_quote_volume: float | None = None
    taker_sell_quote_volume: float | None = None
    taker_buy_trade_count: int | None = None
    taker_sell_trade_count: int | None = None
    taker_buy_agg_trade_count: int | None = None
    taker_sell_agg_trade_count: int | None = None
    max_agg_trade_quantity: float | None = None
    max_taker_buy_agg_trade_quantity: float | None = None
    max_taker_sell_agg_trade_quantity: float | None = None
    first_aggregate_trade_id: int | None = None
    last_aggregate_trade_id: int | None = None
    first_trade_id: int | None = None
    last_trade_id: int | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.timeframe != "1s":
            raise ValueError("Candle1s requires timeframe='1s'")
