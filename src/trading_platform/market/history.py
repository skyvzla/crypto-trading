"""Closed-candle range validation shared by live warmup and archive readers."""

from collections.abc import Iterable

import httpx

from trading_platform.market.recovery import RecoveryCoordinator, RecoveryError
from trading_platform.shared.events import Kline


def closed_kline_window(interval: str, count: int, as_of_ms: int) -> tuple[int, int]:
    if count <= 0:
        raise ValueError("history count must be positive")
    step = RecoveryCoordinator.interval_ms(interval)
    end = as_of_ms // step * step
    return end - count * step, end


def validate_kline_history(
    klines: Iterable[Kline], *, symbol: str, interval: str,
    start_ms: int, end_ms: int, as_of_ms: int,
) -> list[Kline]:
    step = RecoveryCoordinator.interval_ms(interval)
    if start_ms >= end_ms or start_ms % step or end_ms % step:
        raise ValueError("history range must use closed candle boundaries")
    by_time: dict[int, Kline] = {}
    for kline in klines:
        if (kline.symbol != symbol or kline.interval != interval
                or kline.open_time % step
                or kline.close_time != kline.open_time + step - 1
                or kline.available_time < kline.close_time + 1
                or kline.available_time > as_of_ms):
            raise RecoveryError(f"invalid or unavailable {symbol} {interval} candle")
        if not start_ms <= kline.open_time < end_ms:
            raise RecoveryError(f"{symbol} {interval} candle outside requested range")
        previous = by_time.setdefault(kline.open_time, kline)
        if previous.to_dict() != kline.to_dict():
            raise RecoveryError(f"conflicting {symbol} {interval} candle at {kline.open_time}")
    for timestamp in range(start_ms, end_ms, step):
        if timestamp not in by_time:
            raise RecoveryError(f"missing {symbol} {interval} candle at {timestamp}")
    return [by_time[timestamp] for timestamp in sorted(by_time)]


async def fetch_closed_klines(
    client: httpx.AsyncClient, symbol: str, interval: str,
    *, start_ms: int, end_ms: int,
) -> list[Kline]:
    """Fetch a fixed operating window through Market; never slide the cutoff mid-page."""
    step = RecoveryCoordinator.interval_ms(interval)
    result: list[Kline] = []
    for start in range(start_ms, end_ms, 1500 * step):
        end = min(start + 1500 * step, end_ms)
        response = await client.get(
            f"/klines/{symbol}/{interval}",
            params={"start_time": start, "end_time": end - 1, "limit": 1500},
        )
        response.raise_for_status()
        result.extend(validate_kline_history(
            (Kline.from_dict(item) for item in response.json()["klines"]),
            symbol=symbol, interval=interval, start_ms=start, end_ms=end,
            as_of_ms=end_ms,
        ))
    return result
