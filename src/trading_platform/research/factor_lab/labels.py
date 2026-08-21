from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class SpikeLabelConfig:
    """做空视角的未来路径标签。"""

    horizons_seconds: tuple[int, ...] = (300, 900, 1_800, 3_600)
    success_horizon_seconds: int = 1_800
    success_mfe_threshold: float = 0.02
    success_max_mae: float | None = None

    def __post_init__(self) -> None:
        if not self.horizons_seconds or any(value <= 0 for value in self.horizons_seconds):
            raise ValueError("horizons_seconds must contain positive values")
        if self.success_horizon_seconds not in self.horizons_seconds:
            raise ValueError("success horizon must be included in horizons_seconds")
        if self.success_mfe_threshold <= 0:
            raise ValueError("success_mfe_threshold must be positive")
        if self.success_max_mae is not None and self.success_max_mae < 0:
            raise ValueError("success_max_mae cannot be negative")


def horizon_label(seconds: int) -> str:
    if seconds % 3_600 == 0:
        return f"{seconds // 3_600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def attach_short_labels(
    events: pd.DataFrame,
    bars: pd.DataFrame,
    *,
    config: SpikeLabelConfig = SpikeLabelConfig(),
) -> pd.DataFrame:
    """给事件快照附加未来收益、MFE 和 MAE。

    因子只来自事件时点及之前；本函数是唯一允许读取事件之后数据的阶段。
    """
    event_required = {"symbol", "timestamp_ms", "close"}
    bar_required = {"symbol", "timestamp_ms", "close", "high", "low"}
    missing_events = sorted(event_required - set(events.columns))
    missing_bars = sorted(bar_required - set(bars.columns))
    if missing_events:
        raise ValueError(f"events missing label columns: {', '.join(missing_events)}")
    if missing_bars:
        raise ValueError(f"bars missing label columns: {', '.join(missing_bars)}")
    if events.empty:
        return events.copy()

    result = events.copy()
    arrays: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]] = {}
    ordered_bars = bars.sort_values(["symbol", "timestamp_ms"], kind="stable")
    for symbol, group in ordered_bars.groupby("symbol", sort=False):
        arrays[str(symbol)] = (
            group["timestamp_ms"].to_numpy(dtype=np.int64),
            pd.to_numeric(group["close"], errors="coerce").to_numpy(dtype=float),
            pd.to_numeric(group["high"], errors="coerce").to_numpy(dtype=float),
            pd.to_numeric(group["low"], errors="coerce").to_numpy(dtype=float),
        )

    for seconds in config.horizons_seconds:
        suffix = horizon_label(seconds)
        short_returns: list[float] = []
        short_mfes: list[float] = []
        short_maes: list[float] = []
        observations: list[int] = []
        for row in result.itertuples(index=False):
            series = arrays.get(str(row.symbol))
            entry_price = float(row.close)
            if series is None or not np.isfinite(entry_price) or entry_price <= 0:
                short_returns.append(np.nan)
                short_mfes.append(np.nan)
                short_maes.append(np.nan)
                observations.append(0)
                continue
            times, closes, highs, lows = series
            start = np.searchsorted(times, int(row.timestamp_ms) + 1_000, side="left")
            end = np.searchsorted(
                times, int(row.timestamp_ms) + seconds * 1_000, side="right"
            )
            if end <= start:
                short_returns.append(np.nan)
                short_mfes.append(np.nan)
                short_maes.append(np.nan)
                observations.append(0)
                continue
            window_close = closes[start:end]
            window_high = highs[start:end]
            window_low = lows[start:end]
            finite_close = window_close[np.isfinite(window_close)]
            finite_high = window_high[np.isfinite(window_high)]
            finite_low = window_low[np.isfinite(window_low)]
            if not len(finite_close) or not len(finite_high) or not len(finite_low):
                short_returns.append(np.nan)
                short_mfes.append(np.nan)
                short_maes.append(np.nan)
                observations.append(0)
                continue
            final_price = float(finite_close[-1])
            min_price = float(finite_low.min())
            max_price = float(finite_high.max())
            short_returns.append((entry_price - final_price) / entry_price)
            short_mfes.append((entry_price - min_price) / entry_price)
            short_maes.append(max_price / entry_price - 1.0)
            observations.append(end - start)

        result[f"short_return_{suffix}"] = short_returns
        result[f"short_mfe_{suffix}"] = short_mfes
        result[f"short_mae_{suffix}"] = short_maes
        result[f"future_observations_{suffix}"] = observations

    success_suffix = horizon_label(config.success_horizon_seconds)
    success = result[f"short_mfe_{success_suffix}"].ge(config.success_mfe_threshold)
    if config.success_max_mae is not None:
        success &= result[f"short_mae_{success_suffix}"].le(config.success_max_mae)
    success &= result[f"short_mfe_{success_suffix}"].notna()
    result["success"] = success
    return result
