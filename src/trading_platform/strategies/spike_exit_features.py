"""从完成 K 线计算 Spike candidate-v1 退出观测。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np
import pandas as pd

from trading_platform.shared.events import Kline


@dataclass(frozen=True)
class CandidateFeatureConfig:
    fast_slope_bars: int = 5
    slow_slope_bars: int = 15
    volatility_bars: int = 30
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    adx_bars: int = 14
    momentum_change_bars: int = 5
    channel_5m_bars: int = 12
    channel_15m_bars: int = 8
    channel_width_sigma: float = 1.5
    stable_closes: int = 2


def _rolling_log_slope(values: pd.Series, window: int) -> pd.Series:
    x = np.arange(window, dtype=float)
    centered = x - x.mean()
    denominator = float(np.square(centered).sum())
    logs = np.log(values.astype(float))
    return logs.rolling(window, min_periods=window).apply(
        lambda y: float(np.dot(centered, y) / denominator), raw=True
    )


def momentum_indicators(
    candles: pd.DataFrame, config: CandidateFeatureConfig
) -> pd.DataFrame:
    """使用完成的 1m K 线计算因果动能候选。"""
    frame = candles.sort_values("available_ms").reset_index(drop=True).copy()
    close = frame["close"].astype(float)
    log_close = np.log(close)
    returns = log_close.diff()
    volatility = returns.rolling(
        config.volatility_bars, min_periods=config.volatility_bars
    ).std(ddof=0).replace(0, np.nan)
    frame["fast_log_slope_z"] = _rolling_log_slope(
        close, config.fast_slope_bars
    ) / volatility
    frame["slow_log_slope_z"] = _rolling_log_slope(
        close, config.slow_slope_bars
    ) / volatility
    fast_ema = log_close.ewm(span=config.macd_fast, adjust=False).mean()
    slow_ema = log_close.ewm(span=config.macd_slow, adjust=False).mean()
    macd = fast_ema - slow_ema
    signal = macd.ewm(span=config.macd_signal, adjust=False).mean()
    frame["macd_hist_bps"] = (macd - signal) * 10_000
    frame["macd_hist_change_bps"] = frame["macd_hist_bps"].diff(
        config.momentum_change_bars
    )

    high = frame["high"].astype(float)
    low = frame["low"].astype(float)
    previous_close = close.shift(1)
    true_range = pd.concat(
        [high - low, (high - previous_close).abs(), (low - previous_close).abs()],
        axis=1,
    ).max(axis=1)
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = pd.Series(
        np.where((up_move > down_move) & (up_move > 0), up_move, 0.0),
        index=frame.index,
    )
    minus_dm = pd.Series(
        np.where((down_move > up_move) & (down_move > 0), down_move, 0.0),
        index=frame.index,
    )
    alpha = 1 / config.adx_bars
    atr = true_range.ewm(alpha=alpha, adjust=False, min_periods=config.adx_bars).mean()
    plus_di = 100 * plus_dm.ewm(
        alpha=alpha, adjust=False, min_periods=config.adx_bars
    ).mean() / atr
    minus_di = 100 * minus_dm.ewm(
        alpha=alpha, adjust=False, min_periods=config.adx_bars
    ).mean() / atr
    denominator = (plus_di + minus_di).replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / denominator
    frame["adx"] = dx.ewm(
        alpha=alpha, adjust=False, min_periods=config.adx_bars
    ).mean()
    frame["plus_di"] = plus_di
    frame["minus_di"] = minus_di
    frame["adx_change"] = frame["adx"].diff(config.momentum_change_bars)
    frame["minus_di_change"] = frame["minus_di"].diff(
        config.momentum_change_bars
    )
    both_down = (frame["fast_log_slope_z"] < 0) & (frame["slow_log_slope_z"] < 0)
    frame["down_speed_ratio"] = np.where(
        both_down,
        frame["fast_log_slope_z"].abs()
        / frame["slow_log_slope_z"].abs().replace(0, np.nan),
        np.nan,
    )
    frame["slope_decay_probe"] = both_down & (frame["down_speed_ratio"] <= 0.5)
    frame["macd_recovery_probe"] = frame["macd_hist_change_bps"] > 0
    frame["adx_di_decay_probe"] = (
        (frame["minus_di"] > frame["plus_di"])
        & (frame["minus_di_change"] < 0)
        & (frame["adx_change"] < 0)
    )
    frame["decay_probe_agreement"] = frame[
        ["slope_decay_probe", "macd_recovery_probe", "adx_di_decay_probe"]
    ].sum(axis=1)
    return frame


def channel_breakout_candidates(
    candles: pd.DataFrame,
    *,
    lookback: int,
    width_sigma: float,
    stable_closes: int,
) -> pd.DataFrame:
    """用之前的完成 K 线拟合下降通道，并检测连续站稳上轨。"""
    frame = candles.sort_values("available_ms").reset_index(drop=True).copy()
    log_high = np.log(frame["high"].astype(float).to_numpy())
    log_close = np.log(frame["close"].astype(float).to_numpy())
    slope = np.full(len(frame), np.nan)
    upper = np.full(len(frame), np.nan)
    x = np.arange(lookback, dtype=float)
    for index in range(lookback, len(frame)):
        y = log_high[index - lookback : index]
        fitted_slope, intercept = np.polyfit(x, y, 1)
        fitted_sigma = float(np.std(y - (intercept + fitted_slope * x), ddof=0))
        slope[index] = fitted_slope
        upper[index] = intercept + fitted_slope * lookback + width_sigma * fitted_sigma
    frame["channel_slope_bps_per_bar"] = slope * 10_000
    frame["channel_upper"] = np.exp(upper)
    frame["upper_excess_bps"] = (log_close - upper) * 10_000
    frame["channel_break_probe"] = (slope < 0) & (log_close > upper)
    stable = np.zeros(len(frame), dtype=bool)
    stable_excess = np.full(len(frame), np.nan)
    stable_source_slope = np.full(len(frame), np.nan)
    for index in np.flatnonzero(frame["channel_break_probe"].to_numpy()):
        confirmation_index = index + stable_closes - 1
        if confirmation_index >= len(frame):
            continue
        projected = upper[index] + slope[index] * np.arange(stable_closes)
        if np.all(log_close[index : confirmation_index + 1] > projected):
            stable[confirmation_index] = True
            stable_excess[confirmation_index] = (
                log_close[confirmation_index] - projected[-1]
            ) * 10_000
            stable_source_slope[confirmation_index] = slope[index] * 10_000
    frame["stable_breakout_probe"] = stable
    frame["stable_upper_excess_bps"] = stable_excess
    frame["stable_source_slope_bps_per_bar"] = stable_source_slope
    return frame


@dataclass(frozen=True)
class CandidateFeatureSnapshot:
    event_time: int
    decay_agreement: int | None
    stable_breakout_5m: bool
    stable_breakout_15m: bool
    down_channel_5m: bool | None
    down_channel_15m: bool | None


def _frame(klines: Sequence[Kline]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "available_ms": kline.available_time,
                "open": float(kline.open),
                "high": float(kline.high),
                "low": float(kline.low),
                "close": float(kline.close),
                "volume": float(kline.volume),
            }
            for kline in klines
        ]
    )


def candidate_feature_snapshot(
    klines_1m: Sequence[Kline],
    klines_5m: Sequence[Kline],
    klines_15m: Sequence[Kline],
    *,
    config: CandidateFeatureConfig,
) -> CandidateFeatureSnapshot | None:
    """只使用已经完成并可见的 K 线生成最新因果观测。"""
    if not klines_1m:
        return None
    minute = momentum_indicators(_frame(klines_1m), config)
    latest_minute = minute.iloc[-1]
    raw_agreement = latest_minute.get("decay_probe_agreement")
    decay_agreement = None if pd.isna(raw_agreement) else int(raw_agreement)

    def channel_state(
        klines: Sequence[Kline], lookback: int
    ) -> tuple[bool, bool | None]:
        if len(klines) < lookback + config.stable_closes:
            return False, None
        channel = channel_breakout_candidates(
            _frame(klines),
            lookback=lookback,
            width_sigma=config.channel_width_sigma,
            stable_closes=config.stable_closes,
        )
        latest = channel.iloc[-1]
        slope = latest.get("channel_slope_bps_per_bar")
        upper = latest.get("channel_upper")
        in_down_channel = None
        if not pd.isna(slope) and not pd.isna(upper):
            in_down_channel = bool(slope < 0 and latest["close"] <= upper)
        return bool(latest.get("stable_breakout_probe", False)), in_down_channel

    breakout_5m, down_5m = channel_state(klines_5m, config.channel_5m_bars)
    breakout_15m, down_15m = channel_state(klines_15m, config.channel_15m_bars)
    return CandidateFeatureSnapshot(
        event_time=int(latest_minute["available_ms"]),
        decay_agreement=decay_agreement,
        stable_breakout_5m=breakout_5m,
        stable_breakout_15m=breakout_15m,
        down_channel_5m=down_5m,
        down_channel_15m=down_15m,
    )
