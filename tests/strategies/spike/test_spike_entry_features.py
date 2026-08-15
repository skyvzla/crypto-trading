from decimal import Decimal

import pytest

from trading_platform.shared.events import Kline
from trading_platform.strategies.spike.entry_features import (
    entry_context_features,
    td_sell_setup_count,
    upper_wick_ratio,
    volume_multiple,
)


def _klines(
    interval: str, step_ms: int, count: int, *, last_volume: Decimal = Decimal("10")
) -> list[Kline]:
    klines = []
    for index in range(count):
        close = Decimal("100") + Decimal(index)
        volume = last_volume if index == count - 1 else Decimal("10")
        klines.append(
            Kline(
                symbol="XNYUSDT",
                interval=interval,
                open_time=index * step_ms,
                close_time=(index + 1) * step_ms - 1,
                available_time=(index + 1) * step_ms,
                open=close - Decimal("1"),
                high=close + Decimal("3"),
                low=close - Decimal("2"),
                close=close,
                volume=volume,
            )
        )
    return klines


def test_entry_context_uses_only_completed_five_and_fifteen_minute_klines():
    snapshot = entry_context_features(
        _klines("5m", 300_000, 13, last_volume=Decimal("40")),
        _klines("15m", 900_000, 13),
    )

    assert snapshot.td_sell_setup_5m == 9
    assert snapshot.td_sell_setup_15m == 9
    assert snapshot.upper_wick_ratio_5m == Decimal("0.6")
    assert snapshot.upper_wick_ratio_15m == Decimal("0.6")
    assert snapshot.volume_multiple_5m == Decimal("4")


def test_entry_context_rejects_one_minute_klines():
    with pytest.raises(ValueError, match="native 5m"):
        entry_context_features(
            _klines("1m", 60_000, 13),
            _klines("15m", 900_000, 13),
        )


def test_td_setup_refuses_gapped_history_instead_of_guessing():
    klines = _klines("5m", 300_000, 9)
    klines[4] = Kline(
        **{**klines[4].to_dict(), "open_time": 9_999_999, "close_time": 10_299_998}
    )

    assert td_sell_setup_count(klines, interval_ms=300_000) is None


def test_price_features_reject_zero_range_and_missing_volume_history():
    flat = Kline(
        symbol="XNYUSDT",
        interval="5m",
        open_time=0,
        close_time=299_999,
        available_time=300_000,
        open=Decimal("10"),
        high=Decimal("10"),
        low=Decimal("10"),
        close=Decimal("10"),
        volume=Decimal("1"),
    )

    assert upper_wick_ratio(flat) is None
    assert volume_multiple([flat], baseline_bars=12) is None
