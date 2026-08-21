from __future__ import annotations

import numpy as np
import pandas as pd


_REQUIRED_COLUMNS = {
    "symbol",
    "timestamp_ms",
    "open",
    "high",
    "low",
    "close",
    "volume",
}


def _validate_frame(frame: pd.DataFrame) -> None:
    missing = sorted(_REQUIRED_COLUMNS - set(frame.columns))
    if missing:
        raise ValueError(f"1s frame missing columns: {', '.join(missing)}")


def _continuous(group: pd.DataFrame, rows: int) -> pd.Series:
    """当前行与 rows-1 行之前构成连续 1s 窗口。"""
    if rows <= 1:
        return pd.Series(True, index=group.index, dtype=bool)
    expected_ms = (rows - 1) * 1_000
    return group["timestamp_ms"].sub(group["timestamp_ms"].shift(rows - 1)).eq(
        expected_ms
    )


def _safe_ratio(numerator: pd.Series, denominator: pd.Series) -> pd.Series:
    values = numerator.div(denominator.where(denominator > 0))
    return values.replace([np.inf, -np.inf], np.nan)


def add_market_factors(frame: pd.DataFrame) -> pd.DataFrame:
    """增加价格、成交量和形态因子。

    所有滚动统计只使用当前及历史 Bar，不使用未来值。事件触发所需的
    ``rise_5s`` 与 ``volume_multiple_5s`` 与 SpikeSharedFeatureProvider 的
    口径保持一致：5 秒涨幅 + 当前 5 秒成交量 / 前 60 秒中位秒量。
    """
    _validate_frame(frame)
    if frame.empty:
        return frame.copy()

    output: list[pd.DataFrame] = []
    ordered = frame.sort_values(["symbol", "timestamp_ms"], kind="stable")
    for _symbol, source in ordered.groupby("symbol", sort=False):
        group = source.copy().reset_index(drop=True)
        if group["timestamp_ms"].duplicated().any():
            raise ValueError("1s frame contains duplicate symbol/timestamp rows")

        close = pd.to_numeric(group["close"], errors="coerce")
        volume = pd.to_numeric(group["volume"], errors="coerce")

        for seconds in (1, 5, 15, 30, 60, 300):
            shifted = close.shift(seconds)
            continuous = group["timestamp_ms"].sub(
                group["timestamp_ms"].shift(seconds)
            ).eq(seconds * 1_000)
            returns = _safe_ratio(close, shifted).sub(1.0)
            group[f"return_{seconds}s"] = returns.where(continuous)

        continuous_5s = _continuous(group, 5)
        volume_5s = volume.rolling(5, min_periods=5).sum().where(continuous_5s)
        baseline_median = volume.shift(1).rolling(60, min_periods=60).median()
        baseline_mean = volume.shift(1).rolling(60, min_periods=60).mean()
        baseline_std = volume.shift(1).rolling(60, min_periods=60).std(ddof=0)
        continuous_61s = _continuous(group, 61)

        group["continuous_61s"] = continuous_61s
        group["volume_5s"] = volume_5s
        group["median_volume_1s_60s"] = baseline_median.where(continuous_61s)
        group["volume_multiple_5s"] = _safe_ratio(
            volume_5s,
            baseline_median.mul(5.0),
        ).where(continuous_61s)
        mean_volume_5s = volume_5s.div(5.0)
        group["volume_zscore_5s"] = _safe_ratio(
            mean_volume_5s.sub(baseline_mean), baseline_std
        ).where(continuous_61s & baseline_std.gt(0))

        if "quote_volume" in group.columns:
            quote_volume = pd.to_numeric(group["quote_volume"], errors="coerce")
            quote_5s = quote_volume.rolling(5, min_periods=5).sum().where(continuous_5s)
            quote_mean = quote_volume.shift(1).rolling(60, min_periods=60).mean()
            quote_std = quote_volume.shift(1).rolling(60, min_periods=60).std(ddof=0)
            group["quote_volume_5s"] = quote_5s
            group["quote_volume_zscore_5s"] = _safe_ratio(
                quote_5s.div(5.0).sub(quote_mean), quote_std
            ).where(continuous_61s & quote_std.gt(0))

        group["rise_5s"] = group["return_5s"]
        group["price_velocity_5s"] = group["return_5s"].div(5.0)
        group["price_acceleration_5s"] = group["price_velocity_5s"].sub(
            group["price_velocity_5s"].shift(5)
        ).where(
            group["timestamp_ms"].sub(group["timestamp_ms"].shift(10)).eq(10_000)
        )

        high = pd.to_numeric(group["high"], errors="coerce")
        low = pd.to_numeric(group["low"], errors="coerce")
        open_ = pd.to_numeric(group["open"], errors="coerce")
        candle_range = high.sub(low)
        group["upper_wick_ratio_1s"] = _safe_ratio(
            high.sub(pd.concat([open_, close], axis=1).max(axis=1)),
            candle_range,
        ).fillna(0.0)

        output.append(group)

    return pd.concat(output, ignore_index=True)
