"""只用已完成 5m/15m K 线计算的入场顶部环境特征。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from statistics import median
from typing import Sequence

from trading_platform.shared.events import Kline


TD_SETUP_LOOKBACK_BARS = 4
TD_SETUP_MAX = 9
VOLUME_BASELINE_BARS = 12


@dataclass(frozen=True)
class EntryContextFeatures:
    """入场时可见的顶部环境审计值，不直接参与交易决策。"""

    td_sell_setup_5m: int | None
    td_sell_setup_15m: int | None
    upper_wick_ratio_5m: Decimal | None
    upper_wick_ratio_15m: Decimal | None
    volume_multiple_5m: Decimal | None

    def to_audit_details(self) -> dict[str, str | int | None]:
        return {
            "td_sell_setup_5m": self.td_sell_setup_5m,
            "td_sell_setup_15m": self.td_sell_setup_15m,
            "upper_wick_ratio_5m": (
                str(self.upper_wick_ratio_5m)
                if self.upper_wick_ratio_5m is not None
                else None
            ),
            "upper_wick_ratio_15m": (
                str(self.upper_wick_ratio_15m)
                if self.upper_wick_ratio_15m is not None
                else None
            ),
            "volume_multiple_5m": (
                str(self.volume_multiple_5m)
                if self.volume_multiple_5m is not None
                else None
            ),
        }


def entry_context_features(
    klines_5m: Sequence[Kline], klines_15m: Sequence[Kline]
) -> EntryContextFeatures:
    """仅计算当前入场可见的原始 5m/15m K 线特征。"""
    _require_interval(klines_5m, expected_interval="5m")
    _require_interval(klines_15m, expected_interval="15m")
    return EntryContextFeatures(
        td_sell_setup_5m=td_sell_setup_count(klines_5m, interval_ms=300_000),
        td_sell_setup_15m=td_sell_setup_count(klines_15m, interval_ms=900_000),
        upper_wick_ratio_5m=upper_wick_ratio(klines_5m[-1]) if klines_5m else None,
        upper_wick_ratio_15m=(
            upper_wick_ratio(klines_15m[-1]) if klines_15m else None
        ),
        volume_multiple_5m=volume_multiple(
            klines_5m, baseline_bars=VOLUME_BASELINE_BARS
        ),
    )


def _require_interval(
    klines: Sequence[Kline], *, expected_interval: str
) -> None:
    if any(kline.interval != expected_interval for kline in klines):
        raise ValueError(
            f"entry context requires native {expected_interval} klines"
        )


def td_sell_setup_count(
    klines: Sequence[Kline], *, interval_ms: int
) -> int | None:
    """返回末尾连续的 TD 卖出 setup 数；缺失 K 线时拒绝猜测。"""
    if len(klines) <= TD_SETUP_LOOKBACK_BARS:
        return None
    ordered = sorted(klines, key=lambda kline: kline.open_time)
    count = 0
    for index in range(len(ordered) - 1, TD_SETUP_LOOKBACK_BARS - 1, -1):
        current = ordered[index]
        reference = ordered[index - TD_SETUP_LOOKBACK_BARS]
        if current.open_time - reference.open_time != (
            TD_SETUP_LOOKBACK_BARS * interval_ms
        ):
            return None
        if current.close <= reference.close:
            return count
        count += 1
        if count == TD_SETUP_MAX:
            return count
    return count


def upper_wick_ratio(kline: Kline) -> Decimal | None:
    """返回最近一根已完成 K 线的上影线占完整波幅比例。"""
    candle_range = kline.high - kline.low
    if candle_range <= 0:
        return None
    return (kline.high - max(kline.open, kline.close)) / candle_range


def volume_multiple(
    klines: Sequence[Kline], *, baseline_bars: int
) -> Decimal | None:
    """当前已完成 K 线成交量相对之前中位数的倍数。"""
    if len(klines) < baseline_bars + 1:
        return None
    ordered = sorted(klines, key=lambda kline: kline.open_time)
    current = ordered[-1]
    baseline = ordered[-(baseline_bars + 1):-1]
    if any(
        later.open_time - earlier.open_time != current.close_time - current.open_time + 1
        for earlier, later in zip(baseline, baseline[1:])
    ) or current.open_time - baseline[-1].open_time != (
        current.close_time - current.open_time + 1
    ):
        return None
    baseline_median = Decimal(str(median([kline.volume for kline in baseline])))
    if baseline_median <= 0:
        return None
    return current.volume / baseline_median
