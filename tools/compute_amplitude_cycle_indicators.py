#!/usr/bin/env python3
"""计算日振幅周期的多周期指标矩阵。

输入为 ``reports/amplitude/daily_amplitude_cycles_spike_{up,down}.csv``，
行情通过 archive sidecar index + DuckDB ``read_parquet`` 只读读取，衍生指标
通过独立 metrics sidecar 以 ``available_time`` 做 as-of 读取。

输出是一个可直接用 DuckDB/PyArrow 查询的 Parquet 数据集：

``run_id=<id>/``
    events.parquet              原始事件强类型快照
    feature_dictionary.parquet  全部指标、周期、参数、数据源和可用性
    event_features/             事件 start/end/high/low 锚点的宽指标快照
    bars/{1s,5s,1m,5m,15m,1h}/  去掉 warmup 后的逐周期行情及可计算指标
    derivatives/                原始 metrics sidecar 的完整字段
    availability/               每事件/数据源/周期的覆盖与缺口状态
    targets/                    事件结束后的前向收益、MAE/MFE
    failures/                   单 symbol 失败记录
    manifest.json               输入、索引、参数、worker 和输出计数

不在当前归档中的逐笔主动方向、盘口、强平、现货/永续价差和逐价位成交量
分布不会使用 K 线方向伪造，feature_dictionary 会明确标记为
``unsupported_source``。
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import multiprocessing
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Iterable

import duckdb
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from trading_platform.market.archive.index import (  # noqa: E402
    ARCHIVE_INDEX_FILENAME,
    load_archive_index,
    verify_archive_index_files,
)
from trading_platform.market.archive.metrics import (  # noqa: E402
    METRICS_INDEX_FILENAME,
    load_metrics_index,
)
from trading_platform.research.indicators import (  # noqa: E402
    aggregate_ohlcv,
    atr,
    bb_width,
    ema,
    linear_slope_r2,
    macd,
    real_vol,
    roc,
    rsi,
    sma,
    vwap_dev,
)


FORMULA_VERSION = "amplitude-cycle-v1"
MS_1S = 1_000
MS_5S = 5_000
MS_1M = 60_000
MS_5M = 5 * MS_1M
MS_15M = 15 * MS_1M
MS_1H = 60 * MS_1M
MS_DAY = 24 * MS_1H

# Warmup is used for calculation only.  Only event/post rows are persisted.
BACKGROUND_PRE_MS = 72 * MS_1H
METRICS_PRE_MS = 5 * MS_1H
MICRO_PRE_MS = 60 * MS_1S
POST_MS = 30 * MS_1M

TIMEFRAME_MS = {
    "1s": MS_1S,
    "5s": MS_5S,
    "1m": MS_1M,
    "5m": MS_5M,
    "15m": MS_15M,
    "1h": MS_1H,
}
NATIVE_TIMEFRAMES = ("1m", "5m", "15m", "1h")
ALL_BAR_TIMEFRAMES = ("1s", "5s", "1m", "5m", "15m", "1h")
BAR_COLUMNS = [
    "open_ms",
    "close_ms",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "complete_bar",
]


def _feature_specs() -> list[dict[str, Any]]:
    """The complete reference vocabulary, including unavailable sources."""

    specs: list[dict[str, Any]] = []

    def add(
        name: str,
        period: str,
        role: str,
        source: str,
        parameters: str,
        status: str = "supported",
        reason: str | None = None,
    ) -> None:
        specs.append({
            "feature_name": name,
            "period": period,
            "role": role,
            "source_requirements": source,
            "parameters_json": parameters,
            "formula_version": FORMULA_VERSION,
            "status": status,
            "nullable_reason": reason,
            "availability_lag_ms": 1 if period != "1s" else 1_000,
        })

    # Position and environment.
    for name, period, role, params in (
        ("vwap_dev", "5m", "environment", '{"window_bars":20}'),
        ("anchored_vwap_dev", "1m", "trigger", '{"anchor":"running_breakout"}'),
        ("ema_ratio", "5m", "environment", '{"span":20}'),
        ("roc", "5m", "environment", '{"windows_bars":[3,6]}'),
        ("dist_from_recent_high", "1m", "environment", '{"window_minutes":[15,60]}'),
        ("dist_from_session_high", "1m", "environment", '{"session":"utc_day"}'),
        ("range_position", "5m", "environment", '{"window_bars":24}'),
        ("box_up", "1h", "background", '{"window_days":[1,3]}'),
        ("box_dn", "1h", "background", '{"window_days":[1,3]}'),
        ("box_slope", "1h", "background", '{"window_days":[1,3]}'),
        ("box_r2", "1h", "background", '{"window_days":[1,3]}'),
        ("volume_profile_poc", "15m/1h", "background", '{}',),
        ("volume_profile_hvn_lvn", "15m/1h", "background", '{}',),
        ("relative_return_btc", "1m/5m/15m", "environment", '{"windows_minutes":[15,60]}'),
        ("relative_return_sector", "1m/5m/15m", "environment", '{}'),
    ):
        if name.startswith("volume_profile"):
            add(name, period, role, "volume_at_price", params, "unsupported_source", "archive has OHLCV only")
        elif name == "relative_return_sector":
            add(name, period, role, "sector benchmark", params, "unsupported_source", "no timestamped sector map")
        else:
            add(name, period, role, "candles", params)

    # Momentum and volatility.
    for name, period, params in (
        ("rsi", "1m/5m", '{"period":14}'),
        ("macd_hist", "1m/5m", '{"fast":12,"slow":26,"signal":9}'),
        ("macd_hist_change", "1m/5m", '{"lag_bars":3}'),
        ("cci", "5m", '{"period":20}'),
        ("sto_k", "1m/5m", '{"period":14}'),
        ("sto_d", "1m/5m", '{"period":3}'),
        ("adx", "5m/15m", '{"period":14}'),
        ("plus_di", "5m/15m", '{"period":14}'),
        ("minus_di", "5m/15m", '{"period":14}'),
        ("adx_change", "1m/5m", '{"lag_bars":3}'),
        ("di_change", "1m/5m", '{"lag_bars":3}'),
        ("trend_slope", "1m/5m", '{"windows_bars":[15,30]}'),
        ("fast_slow_slope", "1m", '{"fast_bars":5,"slow_bars":15}'),
        ("slope_decay", "1m", '{"volatility_bars":30}'),
        ("bb_width", "1m/5m", '{"period":20,"std":2}'),
        ("bb_width_slope", "1m/5m", '{"lag_bars":5}'),
        ("atr_ratio", "1m/5m", '{"period":14}'),
        ("atr_change", "1m", '{"lag_bars":3}'),
        ("realized_vol", "1m/5m", '{"windows_minutes":[15,60]}'),
        ("volatility_percentile", "1m/5m", '{"baseline_minutes":[60,240]}'),
        ("range_shock_then_contract", "1m", '{"baseline_bars":30,"contract_bars":3}'),
        ("choppiness", "5m/15m", '{"period":14}'),
        ("efficiency_ratio", "5m/15m", '{"period":14}'),
    ):
        add(name, period, "environment" if name not in {"macd_hist_change", "adx_change", "di_change", "slope_decay", "bb_width_slope", "atr_change", "range_shock_then_contract"} else "trigger_aux", "candles", params)

    # Price structure.
    for name, period, role, params in (
        ("failed_breakout", "1m", "trigger", '{"lookback_minutes":15}'),
        ("breakout_retest_failure", "1m", "trigger", '{"retest_bars":5}'),
        ("micro_CHOCH", "1m", "trigger", '{"swing_bars":3}'),
        ("micro_BOS", "1m", "trigger", '{"swing_bars":3}'),
        ("lower_high", "1m", "trigger", '{"swing_bars":3}'),
        ("upper_wick_ratio", "1m/5m", "trigger_aux", '{}'),
        ("body_ratio", "1m/5m", "trigger_aux", '{}'),
        ("close_location_value", "1m/5m", "trigger_aux", '{}'),
        ("high_to_close_retrace", "1m", "trigger_aux", '{}'),
        ("rejection_range", "1m/5m", "trigger_aux", '{}'),
        ("time_at_high", "5s/1m", "trigger_aux", '{"window_seconds":[30,120]}'),
        ("new_high_rate", "5s/1m", "trigger_aux", '{"window_seconds":60}'),
        ("high_progression_decay", "5s/1m", "trigger_aux", '{"window_seconds":60}'),
        ("spike_age", "1s/5s/1m", "environment", '{}'),
        ("retrace_from_spike_high", "1m", "trigger_aux", '{}'),
        ("consecutive_green", "1m/5m", "environment", '{}'),
        ("green_share", "1m/5m", "environment", '{"windows_minutes":[15,60]}'),
        ("hh_count", "1m/5s", "environment", '{"windows_minutes":[3,15]}'),
        ("td_sell_setup", "5m/15m", "auxiliary", '{"reference_bars":4,"max_count":9}'),
    ):
        add(name, period, role, "candles", params)

    # Flow and microstructure.  These are deliberately not filled by a candle proxy.
    for name, period, role, source in (
        ("taker_buy_ratio", "5s/1m", "environment", "aggTrades or taker fields"),
        ("taker_delta", "1s/5s/1m", "trigger_aux", "aggTrades"),
        ("signed_volume_delta", "1s/5s/1m", "trigger_aux", "aggTrades"),
        ("cvd", "5s/1m", "trigger", "aggTrades"),
        ("price_cvd_divergence", "1m", "trigger", "aggTrades"),
        ("delta_price_efficiency", "5s/1m", "trigger_aux", "aggTrades"),
        ("price_impact", "5s/1m", "trigger_aux", "aggTrades"),
        ("buy_absorption", "1s/5s", "trigger", "order book + aggTrades"),
        ("order_book_imbalance", "1s/5s", "trigger_aux", "order book"),
        ("ask_replenishment", "1s/5s", "trigger_aux", "order book"),
    ):
        add(name, period, role, source, '{}', "unsupported_source", f"missing {source} archive")
    for name, period, role, params in (
        ("volume_climax_then_decay", "5s/1m", "trigger", '{"baseline_bars":60,"decay_bars":3}'),
        ("volume_multiple", "5s/1m/5m", "environment", '{"baseline_bars":[12,60]}'),
        ("vol_zscore", "1m/5m", "environment", '{"baseline_bars":60}'),
        ("obv_slope", "1m/5m", "auxiliary", '{"lookback_bars":20}'),
        ("mfi", "1m/5m", "auxiliary", '{"period":14}'),
        ("up_down_volume_ratio", "1m/5m", "auxiliary", '{"window_minutes":[15,60]}'),
        ("vol_cv", "1m", "auxiliary", '{"window_minutes":[30,60]}'),
        ("intrabar_range_share", "1s/5s", "trigger_aux", '{"window_seconds":60}'),
    ):
        add(name, period, role, "candles", params)

    # Derivatives and cross-market fields.
    for name, period, role, params in (
        ("open_interest_delta", "1m/5m", "environment", '{"windows_minutes":[1,5,15]}'),
        ("oi_price_quadrant", "1m/5m", "environment", '{}'),
        ("oi_acceleration", "1m/5m", "trigger_aux", '{"lag_windows":3}'),
        ("long_short_ratio", "5m/15m/1h", "background", '{}'),
    ):
        add(name, period, role, "metrics sidecar", params)
    for name, source in (
        ("liquidation_burst", "forceOrder"),
        ("short_liquidation_share", "forceOrder"),
        ("liquidation_decay", "forceOrder"),
        ("basis", "spot + perpetual"),
        ("mark_index_premium", "mark + index"),
        ("spot_perp_divergence", "spot + perpetual"),
    ):
        add(name, "1s/5s/1m" if name.startswith("liquidation") else "1m/5m", "trigger_aux", source, '{}', "unsupported_source", f"missing {source} archive")
    return specs


FEATURE_SPECS = _feature_specs()


def _safe_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _clean_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame(columns=BAR_COLUMNS)
    result = frame.copy()
    for column in ("open_ms", "close_ms"):
        result[column] = pd.to_numeric(result[column], errors="coerce").astype("Int64")
    for column in ("open", "high", "low", "close", "volume"):
        result[column] = pd.to_numeric(result[column], errors="coerce")
    result = result.dropna(subset=["open_ms", "close_ms", "open", "high", "low", "close", "volume"])
    result["open_ms"] = result["open_ms"].astype(np.int64)
    result["close_ms"] = result["close_ms"].astype(np.int64)
    result = result.sort_values("open_ms").drop_duplicates("open_ms").reset_index(drop=True)
    result["complete_bar"] = True
    return result[BAR_COLUMNS]


def _query_candles(
    connection: duckdb.DuckDBPyConnection,
    paths: list[str],
    symbol: str,
    timeframe: str,
    start_ms: int,
    end_ms: int,
) -> pd.DataFrame:
    if not paths:
        return pd.DataFrame(columns=BAR_COLUMNS)
    rows = connection.execute(
        """
        SELECT epoch_ms(open_time) AS open_ms,
               epoch_ms(close_time) AS close_ms,
               open, high, low, close, volume
        FROM read_parquet(?, union_by_name=true)
        WHERE symbol = ? AND timeframe = ?
          AND epoch_ms(open_time) < ?
          AND epoch_ms(close_time) >= ?
        ORDER BY open_time
        """,
        [paths, symbol, timeframe, end_ms, start_ms],
    ).fetchall()
    return _clean_frame(pd.DataFrame(
        rows,
        columns=["open_ms", "close_ms", "open", "high", "low", "close", "volume"],
    ))


def _merge_intervals(intervals: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start_ms, end_ms in sorted(intervals):
        if start_ms >= end_ms:
            continue
        if merged and start_ms <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end_ms))
        else:
            merged.append((start_ms, end_ms))
    return merged


def _query_candle_intervals(
    connection: duckdb.DuckDBPyConnection,
    paths: list[str],
    symbol: str,
    timeframe: str,
    intervals: Iterable[tuple[int, int]],
) -> pd.DataFrame:
    parts = [
        _query_candles(connection, paths, symbol, timeframe, start_ms, end_ms)
        for start_ms, end_ms in _merge_intervals(intervals)
    ]
    parts = [part for part in parts if not part.empty]
    if not parts:
        return pd.DataFrame(columns=BAR_COLUMNS)
    return _clean_frame(pd.concat(parts, ignore_index=True))


def _aggregate_frame(frame: pd.DataFrame, timeframe: str, source_timeframe: str) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame(columns=BAR_COLUMNS)
    tf_ms = TIMEFRAME_MS[timeframe]
    source_ms = TIMEFRAME_MS[source_timeframe]
    data = frame.copy()
    data["bar_open_ms"] = (data["open_ms"] // tf_ms) * tf_ms
    grouped = data.groupby("bar_open_ms", sort=True, observed=True)
    out = grouped.agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
        input_count=("open_ms", "size"),
    ).reset_index(names="open_ms")
    expected = max(1, tf_ms // source_ms)
    out["complete_bar"] = out["input_count"] >= expected
    out["close_ms"] = out["open_ms"] + tf_ms - 1
    return out[BAR_COLUMNS].reset_index(drop=True)


def _rolling_slope(values: np.ndarray, window: int) -> np.ndarray:
    out = np.full(len(values), np.nan)
    if window < 2:
        return out
    xs = np.arange(window, dtype=float)
    x_mean = xs.mean()
    x_var = np.sum((xs - x_mean) ** 2)
    for i in range(window - 1, len(values)):
        segment = values[i - window + 1 : i + 1]
        if not np.isfinite(segment).all() or np.any(segment <= 0):
            continue
        ys = np.log(segment)
        out[i] = float(np.sum((xs - x_mean) * (ys - ys.mean())) / x_var)
    return out


def _rolling_std(values: np.ndarray, window: int) -> np.ndarray:
    return pd.Series(values).rolling(window, min_periods=window).std(ddof=1).to_numpy()


def _rolling_quantile(values: np.ndarray, window: int, q: float) -> np.ndarray:
    return pd.Series(values).rolling(window, min_periods=window).quantile(q).to_numpy()


def _adx_di(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    n = len(close)
    tr = np.zeros(n)
    plus_dm = np.zeros(n)
    minus_dm = np.zeros(n)
    prev = np.roll(close, 1)
    prev[0] = close[0] if n else 0.0
    for i in range(1, n):
        tr[i] = max(high[i] - low[i], abs(high[i] - prev[i]), abs(low[i] - prev[i]))
        up = high[i] - high[i - 1]
        down = low[i - 1] - low[i]
        plus_dm[i] = up if up > down and up > 0 else 0.0
        minus_dm[i] = down if down > up and down > 0 else 0.0
    atr_s = pd.Series(tr).ewm(alpha=1 / period, adjust=False, min_periods=period).mean().to_numpy()
    plus_s = pd.Series(plus_dm).ewm(alpha=1 / period, adjust=False, min_periods=period).mean().to_numpy()
    minus_s = pd.Series(minus_dm).ewm(alpha=1 / period, adjust=False, min_periods=period).mean().to_numpy()
    plus_di = 100 * plus_s / np.where(atr_s == 0, np.nan, atr_s)
    minus_di = 100 * minus_s / np.where(atr_s == 0, np.nan, atr_s)
    dx = 100 * np.abs(plus_di - minus_di) / np.where(plus_di + minus_di == 0, np.nan, plus_di + minus_di)
    adx = pd.Series(dx).ewm(alpha=1 / period, adjust=False, min_periods=period).mean().to_numpy()
    return adx, plus_di, minus_di


def _mfi(high: np.ndarray, low: np.ndarray, close: np.ndarray, volume: np.ndarray, period: int) -> np.ndarray:
    tp = (high + low + close) / 3.0
    raw = tp * volume
    sign = np.sign(np.diff(tp, prepend=tp[0]))
    pos = pd.Series(np.where(sign > 0, raw, 0.0)).rolling(period, min_periods=period).sum().to_numpy()
    neg = pd.Series(np.where(sign < 0, raw, 0.0)).rolling(period, min_periods=period).sum().to_numpy()
    ratio = pos / np.where(neg == 0, np.nan, neg)
    return 100 - 100 / (1 + ratio)


def _choppiness(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int) -> np.ndarray:
    prev = np.roll(close, 1)
    if len(prev):
        prev[0] = close[0]
    tr = np.maximum(high - low, np.maximum(np.abs(high - prev), np.abs(low - prev)))
    tr_sum = pd.Series(tr).rolling(period, min_periods=period).sum().to_numpy()
    hh = pd.Series(high).rolling(period, min_periods=period).max().to_numpy()
    ll = pd.Series(low).rolling(period, min_periods=period).min().to_numpy()
    return 100 * np.log10(tr_sum / np.where(hh - ll == 0, np.nan, hh - ll)) / np.log10(period)


def _efficiency(close: np.ndarray, period: int) -> np.ndarray:
    change = np.abs(close - np.roll(close, period))
    change[:period] = np.nan
    noise = pd.Series(np.abs(np.diff(close, prepend=close[0]))).rolling(period, min_periods=period).sum().to_numpy()
    return change / np.where(noise == 0, np.nan, noise)


def _series_slope(values: np.ndarray, window: int) -> np.ndarray:
    out = np.full(len(values), np.nan)
    if window < 2:
        return out
    xs = np.arange(window, dtype=float)
    for i in range(window - 1, len(values)):
        segment = values[i - window + 1 : i + 1]
        if np.isfinite(segment).all():
            out[i] = np.polyfit(xs, segment, 1)[0]
    return out


def _add_bar_features(frame: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """Add OHLCV-computable columns; no unsupported data is substituted."""

    if frame.empty:
        return frame.copy()
    out = frame.copy().reset_index(drop=True)
    n = len(out)
    o = out["open"].to_numpy(float)
    h = out["high"].to_numpy(float)
    l = out["low"].to_numpy(float)
    c = out["close"].to_numpy(float)
    v = out["volume"].to_numpy(float)
    rng = np.maximum(h - l, 1e-12)
    tf_minutes = max(1, TIMEFRAME_MS[timeframe] // MS_1M)
    fast_window = max(3, round(5 / tf_minutes))
    slow_window = max(fast_window + 1, round(15 / tf_minutes))
    vol_window = max(10, round(30 / tf_minutes))
    short_window = max(3, round(15 / tf_minutes))

    out["upper_wick_ratio"] = (h - np.maximum(o, c)) / rng
    out["body_ratio"] = np.abs(c - o) / rng
    out["close_location_value"] = (2 * c - h - l) / rng
    out["high_to_close_retrace"] = (h / np.maximum(c, 1e-12) - 1) * 100
    out["ema_ratio"] = c / np.maximum(ema(c, 20), 1e-12)
    out["roc"] = roc(c, max(1, round(5 / tf_minutes)))
    out["rsi"] = rsi(c, 14)
    _, _, macd_hist = macd(c, 12, 26, 9)
    out["macd_hist"] = macd_hist
    out["macd_hist_change"] = macd_hist - np.roll(macd_hist, max(1, round(3 / tf_minutes)))
    out.loc[: max(1, round(3 / tf_minutes)) - 1, "macd_hist_change"] = np.nan
    out["cci"] = np.nan
    tp = (h + l + c) / 3.0
    cci_period = 20
    for i in range(cci_period - 1, n):
        segment = tp[i - cci_period + 1 : i + 1]
        mad = np.mean(np.abs(segment - segment.mean()))
        if mad > 0:
            out.at[i, "cci"] = (segment[-1] - segment.mean()) / (0.015 * mad)
    stoch_period = 14
    low_roll = pd.Series(l).rolling(stoch_period, min_periods=stoch_period).min().to_numpy()
    high_roll = pd.Series(h).rolling(stoch_period, min_periods=stoch_period).max().to_numpy()
    out["sto_k"] = (c - low_roll) / np.where(high_roll - low_roll == 0, np.nan, high_roll - low_roll) * 100
    out["sto_d"] = pd.Series(out["sto_k"]).rolling(3, min_periods=3).mean().to_numpy()
    adx, plus_di, minus_di = _adx_di(h, l, c, 14)
    out["adx"] = adx
    out["plus_di"] = plus_di
    out["minus_di"] = minus_di
    change_lag = max(1, round(3 / tf_minutes))
    out["adx_change"] = adx - np.roll(adx, change_lag)
    out["di_change"] = (plus_di - minus_di) - np.roll(plus_di - minus_di, change_lag)
    out.loc[: change_lag - 1, ["adx_change", "di_change"]] = np.nan
    out["trend_slope"] = _rolling_slope(c, short_window)
    out["fast_slow_slope"] = _rolling_slope(c, fast_window) - _rolling_slope(c, slow_window)
    fast = _rolling_slope(c, fast_window)
    slow = _rolling_slope(c, slow_window)
    out["slope_decay"] = fast / np.where(np.abs(slow) < 1e-12, np.nan, np.abs(slow))
    _, _, _, width = bb_width(c, 20, 2.0)
    out["bb_width"] = width
    bb_lag = max(1, round(5 / tf_minutes))
    out["bb_width_slope"] = width - np.roll(width, bb_lag)
    out.loc[: bb_lag - 1, "bb_width_slope"] = np.nan
    atr_values = atr(h, l, c, 14)
    out["atr_ratio"] = atr_values / np.maximum(c, 1e-12)
    atr_lag = max(1, round(3 / tf_minutes))
    out["atr_change"] = out["atr_ratio"] - np.roll(out["atr_ratio"].to_numpy(float), atr_lag)
    out.loc[: atr_lag - 1, "atr_change"] = np.nan
    log_ret = np.diff(np.log(np.maximum(c, 1e-12)), prepend=np.log(max(c[0], 1e-12)))
    rv_window = max(5, round(15 / tf_minutes))
    out["realized_vol"] = _rolling_std(log_ret, rv_window)
    baseline_window = max(rv_window * 2, round(60 / tf_minutes))
    rv_series = pd.Series(out["realized_vol"])
    out["volatility_percentile"] = rv_series.rolling(baseline_window, min_periods=baseline_window).rank(pct=True).to_numpy()
    prev_close = np.roll(c, 1)
    prev_close[0] = c[0]
    true_range = np.maximum(h - l, np.maximum(np.abs(h - prev_close), np.abs(l - prev_close)))
    tr_median = pd.Series(true_range).rolling(vol_window, min_periods=vol_window).median().to_numpy()
    range_shock = true_range > 2.5 * np.where(tr_median == 0, np.nan, tr_median)
    out["range_shock_then_contract"] = (
        pd.Series(range_shock).shift(1, fill_value=False).astype(bool)
        & (pd.Series(true_range).rolling(3, min_periods=3).max() < pd.Series(true_range).shift(1).rolling(3, min_periods=3).max())
        & (c <= pd.Series(h).rolling(3, min_periods=3).max().to_numpy())
    ).astype(np.int8)
    out["choppiness"] = _choppiness(h, l, c, 14)
    out["efficiency_ratio"] = _efficiency(c, 14)

    lookback = max(3, round(15 / tf_minutes))
    prior_high = pd.Series(h).shift(1).rolling(lookback, min_periods=lookback).max()
    prior_low = pd.Series(l).shift(1).rolling(3, min_periods=3).min()
    out["failed_breakout"] = ((h > prior_high) & (c < prior_high)).astype(np.int8)
    breakout = h > prior_high
    out["breakout_retest_failure"] = (
        breakout.shift(1, fill_value=False)
        & (h >= prior_high.shift(1))
        & (c < prior_high.shift(1))
    ).astype(np.int8)
    high_series = pd.Series(h)
    out["micro_CHOCH"] = ((c < prior_low) & (high_series.shift(1) > high_series.shift(2))).astype(np.int8)
    out["micro_BOS"] = (c < prior_low).astype(np.int8)
    out["lower_high"] = (h < pd.Series(h).shift(1).rolling(3, min_periods=3).max()).astype(np.int8)
    out["rejection_range"] = (
        (out["upper_wick_ratio"] >= 0.5)
        & (out["close_location_value"] <= 0)
        & (out["volume"] >= pd.Series(v).rolling(vol_window, min_periods=vol_window).median())
    ).astype(np.int8)

    high_window = max(3, round(15 / tf_minutes))
    rolling_high = pd.Series(h).rolling(high_window, min_periods=high_window).max()
    out["dist_from_recent_high"] = c / np.maximum(rolling_high.to_numpy(), 1e-12) - 1
    day_key = pd.Series(out["open_ms"] // MS_DAY)
    session_high = pd.Series(h).groupby(day_key, sort=False).cummax().to_numpy()
    out["dist_from_session_high"] = c / np.maximum(session_high, 1e-12) - 1
    out["range_position"] = (c - pd.Series(l).rolling(high_window, min_periods=high_window).min().to_numpy()) / np.maximum(
        pd.Series(h).rolling(high_window, min_periods=high_window).max().to_numpy()
        - pd.Series(l).rolling(high_window, min_periods=high_window).min().to_numpy(), 1e-12
    )
    out["spike_age_bars"] = _bars_since_true(h >= rolling_high.to_numpy())
    out["retrace_from_spike_high"] = c / np.maximum(rolling_high.to_numpy(), 1e-12) - 1
    green = c > o
    out["consecutive_green"] = _consecutive_count(green)
    out["green_share"] = pd.Series(green.astype(float)).rolling(high_window, min_periods=high_window).mean().to_numpy()
    out["hh_count"] = pd.Series((h >= rolling_high.to_numpy()).astype(float)).rolling(high_window, min_periods=high_window).sum().to_numpy()
    td_cond = c > pd.Series(c).shift(4)
    out["td_sell_setup"] = _consecutive_count(td_cond.fillna(False).to_numpy())

    typical = (o + h + l + c) / 4.0
    volume_delta_proxy = np.sign(c - o) * v
    out["signed_volume_delta_ohlcv_proxy"] = volume_delta_proxy
    out["cvd_ohlcv_proxy"] = np.cumsum(volume_delta_proxy)
    out["volume_multiple"] = v / np.maximum(pd.Series(v).rolling(max(12, vol_window), min_periods=max(12, vol_window)).median().to_numpy(), 1e-12)
    mean_v = pd.Series(v).rolling(vol_window, min_periods=vol_window).mean().to_numpy()
    std_v = pd.Series(v).rolling(vol_window, min_periods=vol_window).std(ddof=1).to_numpy()
    out["vol_zscore"] = (v - mean_v) / np.where(std_v == 0, np.nan, std_v)
    out["delta_price_efficiency_ohlcv_proxy"] = np.abs(log_ret) / np.maximum(np.abs(volume_delta_proxy), 1e-12)
    out["price_impact_ohlcv_proxy"] = np.abs(log_ret) / np.maximum(v, 1e-12)
    # 只使用当前及历史数据：前一根出现量能峰值，当前量能衰减且价格未继续创新高。
    volume_series = pd.Series(v)
    high_series = pd.Series(h)
    prior_volume_peak = (
        volume_series.shift(1)
        >= volume_series.shift(1).rolling(vol_window, min_periods=vol_window).quantile(0.95)
    )
    prior_high = high_series.shift(1).rolling(3, min_periods=3).max()
    out["volume_climax_then_decay"] = (
        prior_volume_peak
        & (volume_series < volume_series.shift(1))
        & (high_series <= prior_high)
    ).fillna(False).astype(np.int8).to_numpy()
    out["obv_slope"] = _obv_slope_array(c, v, max(5, min(20, n)))
    out["mfi"] = _mfi(h, l, c, v, 14)
    up_volume = np.where(c > o, v, 0.0)
    down_volume = np.where(c < o, v, 0.0)
    out["up_down_volume_ratio"] = pd.Series(up_volume).rolling(high_window, min_periods=high_window).sum().to_numpy() / np.maximum(
        pd.Series(down_volume).rolling(high_window, min_periods=high_window).sum().to_numpy(), 1e-12
    )
    out["vol_cv"] = pd.Series(v).rolling(vol_window, min_periods=vol_window).std(ddof=1).to_numpy() / np.maximum(
        pd.Series(v).rolling(vol_window, min_periods=vol_window).mean().to_numpy(), 1e-12
    )
    range_sum = pd.Series(rng).rolling(max(1, round(60 / tf_minutes)), min_periods=max(1, round(60 / tf_minutes))).sum().to_numpy()
    out["intrabar_range_share"] = rng / np.maximum(range_sum, 1e-12)
    out["vwap_dev"] = np.nan
    for i in range(n):
        window = min(20, i + 1)
        if window > 0:
            out.at[i, "vwap_dev"] = vwap_dev(o[: i + 1], h[: i + 1], l[: i + 1], c[: i + 1], v[: i + 1], window)
    return out


def _bars_since_true(values: np.ndarray) -> np.ndarray:
    out = np.full(len(values), np.nan)
    count = np.nan
    for i, value in enumerate(values):
        if bool(value):
            count = 0
        elif np.isfinite(count):
            count += 1
        out[i] = count
    return out


def _consecutive_count(values: np.ndarray) -> np.ndarray:
    out = np.zeros(len(values), dtype=float)
    for i, value in enumerate(values):
        out[i] = out[i - 1] + 1 if i and bool(value) else (1 if bool(value) else 0)
    return out


def _obv_slope_array(close: np.ndarray, volume: np.ndarray, window: int) -> np.ndarray:
    signed = np.sign(np.diff(close, prepend=close[0])) * volume
    obv = np.cumsum(signed)
    return _series_slope(obv, window)


def _aggregate_5s(frame_1s: pd.DataFrame) -> pd.DataFrame:
    return _aggregate_frame(frame_1s, "5s", "1s")


def _event_id(source_file: str, source_row: int, symbol: str, start_ms: int, end_ms: int) -> str:
    raw = f"{source_file}\0{source_row}\0{symbol}\0{start_ms}\0{end_ms}".encode()
    return hashlib.sha256(raw).hexdigest()[:24]


def _parse_events(paths: list[Path]) -> pd.DataFrame:
    frames = []
    for path in paths:
        frame = pd.read_csv(path)
        frame["source_file"] = str(path)
        frame["source_row"] = np.arange(len(frame), dtype=np.int64)
        frames.append(frame)
    if not frames:
        raise ValueError("no event files supplied")
    events = pd.concat(frames, ignore_index=True)
    required = {"symbol", "start_utc", "end_utc"}
    missing = required - set(events.columns)
    if missing:
        raise ValueError(f"event CSV missing columns: {sorted(missing)}")
    events["symbol"] = events["symbol"].astype(str).str.strip().str.upper()
    def timestamp_ms(values: pd.Series) -> pd.Series:
        parsed = pd.to_datetime(values, utc=True, errors="coerce")
        result = pd.Series(pd.NA, index=values.index, dtype="Int64")
        valid = parsed.notna()
        result.loc[valid] = parsed.loc[valid].astype("int64") // 1_000_000
        return result

    events["start_ms"] = timestamp_ms(events["start_utc"])
    events["end_ms"] = timestamp_ms(events["end_utc"])
    if "high_utc" in events:
        events["high_ms"] = timestamp_ms(events["high_utc"])
    else:
        events["high_ms"] = pd.Series(pd.NA, index=events.index, dtype="Int64")
    if "low_utc" in events:
        events["low_ms"] = timestamp_ms(events["low_utc"])
    else:
        events["low_ms"] = pd.Series(pd.NA, index=events.index, dtype="Int64")
    valid = events["symbol"].ne("") & events["start_ms"].notna() & events["end_ms"].notna() & (events["end_ms"] > events["start_ms"])
    events = events[valid].copy()
    events["event_id"] = [
        _event_id(str(row.source_file), int(row.source_row), str(row.symbol), int(row.start_ms), int(row.end_ms))
        for row in events.itertuples()
    ]
    events["source_direction"] = events["source_file"].map(lambda value: "down" if "_down" in str(value) else "up")
    return events.reset_index(drop=True)


def _select_paths(index: pd.DataFrame, root: Path, symbol: str, timeframe: str, start_ms: int, end_ms: int) -> list[str]:
    selected = index[
        (index["symbol"] == symbol)
        & (index["timeframe"] == timeframe)
        & (index["first_open_ms"] < end_ms)
        & (index["last_close_ms"] >= start_ms)
    ].drop_duplicates("relative_path")
    return [str(root / path) for path in selected["relative_path"].tolist()]


def _select_paths_for_intervals(
    index: pd.DataFrame,
    root: Path,
    symbol: str,
    timeframe: str,
    intervals: Iterable[tuple[int, int]],
) -> list[str]:
    paths: set[str] = set()
    for start_ms, end_ms in _merge_intervals(intervals):
        paths.update(_select_paths(index, root, symbol, timeframe, start_ms, end_ms))
    return sorted(paths)


def _select_metric_paths(index: pd.DataFrame, root: Path, symbol: str, start_ms: int, end_ms: int) -> list[str]:
    selected = index[
        (index["symbol"] == symbol)
        & (index["period"] == "5m")
        & (index["first_snapshot_ms"] < end_ms)
        & (index["last_snapshot_ms"] >= start_ms)
    ].drop_duplicates("relative_path")
    return [str(root / path) for path in selected["relative_path"].tolist()]


def _select_metric_paths_for_intervals(
    index: pd.DataFrame,
    root: Path,
    symbol: str,
    intervals: Iterable[tuple[int, int]],
) -> list[str]:
    paths: set[str] = set()
    for start_ms, end_ms in _merge_intervals(intervals):
        paths.update(_select_metric_paths(index, root, symbol, start_ms, end_ms))
    return sorted(paths)


def _query_metrics(connection: duckdb.DuckDBPyConnection, paths: list[str], symbol: str, start_ms: int, end_ms: int) -> pd.DataFrame:
    columns = [
        "available_ms", "snapshot_ms", "sum_open_interest", "sum_open_interest_value",
        "count_toptrader_long_short_ratio", "sum_toptrader_long_short_ratio",
        "count_long_short_ratio", "sum_taker_long_short_vol_ratio",
    ]
    if not paths:
        return pd.DataFrame(columns=columns)
    rows = connection.execute(
        """
        SELECT epoch_ms(available_time), epoch_ms(snapshot_time),
               sum_open_interest, sum_open_interest_value,
               count_toptrader_long_short_ratio, sum_toptrader_long_short_ratio,
               count_long_short_ratio, sum_taker_long_short_vol_ratio
        FROM read_parquet(?, union_by_name=true)
        WHERE symbol = ? AND period = '5m'
          AND available_time IS NOT NULL
          AND epoch_ms(available_time) >= ?
          AND epoch_ms(available_time) < ?
        ORDER BY available_time, snapshot_time
        """,
        [paths, symbol, start_ms, end_ms],
    ).fetchall()
    result = pd.DataFrame(rows, columns=columns)
    if result.empty:
        return result
    result = result.drop_duplicates(["available_ms", "snapshot_ms"]).sort_values("available_ms").reset_index(drop=True)
    return result


def _query_metric_intervals(
    connection: duckdb.DuckDBPyConnection,
    paths: list[str],
    symbol: str,
    intervals: Iterable[tuple[int, int]],
) -> pd.DataFrame:
    parts = [
        _query_metrics(connection, paths, symbol, start_ms, end_ms)
        for start_ms, end_ms in _merge_intervals(intervals)
    ]
    parts = [part for part in parts if not part.empty]
    if not parts:
        return pd.DataFrame(columns=[
            "available_ms", "snapshot_ms", "sum_open_interest",
            "sum_open_interest_value", "count_toptrader_long_short_ratio",
            "sum_toptrader_long_short_ratio", "count_long_short_ratio",
            "sum_taker_long_short_vol_ratio",
        ])
    return (
        pd.concat(parts, ignore_index=True)
        .drop_duplicates(["available_ms", "snapshot_ms"])
        .sort_values("available_ms")
        .reset_index(drop=True)
    )


def _merge_metrics(frame: pd.DataFrame, metrics: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()
    result = frame.copy().sort_values("close_ms")
    if metrics.empty:
        for col in metrics.columns:
            if col not in result:
                result[col] = np.nan
        return result
    metric_cols = [c for c in metrics.columns if c not in {"available_ms", "snapshot_ms"}]
    result = pd.merge_asof(
        result,
        metrics.sort_values("available_ms"),
        left_on="close_ms",
        right_on="available_ms",
        direction="backward",
        allow_exact_matches=True,
    )
    for col in metric_cols:
        if col in result:
            result[col] = pd.to_numeric(result[col], errors="coerce")
    if "sum_open_interest" in result:
        result["open_interest"] = result["sum_open_interest"]
        result["open_interest_delta"] = result["open_interest"].diff()
        result["open_interest_delta_pct"] = result["open_interest"].pct_change() * 100
        result["oi_acceleration"] = result["open_interest_delta"].diff()
        price_change = result["close"].pct_change()
        result["oi_price_quadrant"] = np.select(
            [
                (price_change > 0) & (result["open_interest_delta"] < 0),
                (price_change > 0) & (result["open_interest_delta"] > 0),
                (price_change < 0) & (result["open_interest_delta"] > 0),
                (price_change < 0) & (result["open_interest_delta"] < 0),
            ],
            ["price_up_oi_down", "price_up_oi_up", "price_down_oi_up", "price_down_oi_down"],
            default=None,
        )
    # 保留底层 metrics 原始列，同时提供指标字典使用的稳定别名。
    aliases = {
        "long_short_ratio": "count_long_short_ratio",
        "toptrader_long_short_ratio": "count_toptrader_long_short_ratio",
        "taker_long_short_vol_ratio": "sum_taker_long_short_vol_ratio",
    }
    for alias, source in aliases.items():
        if source in result:
            result[alias] = pd.to_numeric(result[source], errors="coerce")
    return result


def _asof_value(metrics: pd.DataFrame, timestamp_ms: int, column: str) -> float | None:
    if metrics.empty:
        return None
    rows = metrics[metrics["available_ms"] <= timestamp_ms]
    if rows.empty or column not in rows:
        return None
    return _safe_float(rows.iloc[-1][column])


def _box_features(frame_1h: pd.DataFrame) -> dict[str, float | None]:
    if frame_1h.empty:
        return {"box_up": None, "box_dn": None, "box_slope": None, "box_r2": None}
    frame = frame_1h.tail(72)
    high = frame["high"].to_numpy(float)
    low = frame["low"].to_numpy(float)
    if len(high) < 8:
        return {"box_up": None, "box_dn": None, "box_slope": None, "box_r2": None}
    slope, r2 = linear_slope_r2(high)
    return {
        "box_up": _safe_float(np.percentile(high, 90)),
        "box_dn": _safe_float(np.percentile(low, 10)),
        "box_slope": _safe_float(slope),
        "box_r2": _safe_float(r2),
    }


def _event_scalar_features(
    event: dict[str, Any],
    frames: dict[str, pd.DataFrame],
    metrics: pd.DataFrame,
    anchor_ms: int,
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    start_ms = int(event["start_ms"])
    one_min = frames.get("1m", pd.DataFrame())
    five_sec = frames.get("5s", pd.DataFrame())
    one_hour = frames.get("1h", pd.DataFrame())
    # 起点之前的背景只允许使用已经收盘的完整 1h K 线。
    result.update(_box_features(one_hour[one_hour["close_ms"] + 1 <= start_ms]))
    result["anchored_vwap_dev"] = None
    result["time_at_high"] = None
    result["new_high_rate"] = None
    result["high_progression_decay"] = None
    result["intrabar_range_share"] = None
    result["spike_age"] = None
    result["retrace_from_spike_high"] = None
    if not one_min.empty:
        before = one_min[one_min["close_ms"] + 1 <= anchor_ms]
        if not before.empty:
            # Anchor VWAP from the event start using only bars visible by end.
            anchored = before[before["open_ms"] >= start_ms]
            if not anchored.empty:
                typical = (anchored["open"] + anchored["high"] + anchored["low"] + anchored["close"]) / 4
                denominator = float(anchored["volume"].sum())
                if denominator > 0:
                    result["anchored_vwap_dev"] = _safe_float((float(anchored.iloc[-1]["close"]) / float((typical * anchored["volume"]).sum() / denominator) - 1) * 100)
            recent = before.tail(60)
            rolling_high = float(recent["high"].max()) if not recent.empty else None
            if rolling_high and rolling_high > 0:
                result["retrace_from_spike_high"] = _safe_float(float(recent.iloc[-1]["close"]) / rolling_high - 1)
                high_time = int(recent.loc[recent["high"].idxmax(), "open_ms"])
                result["spike_age"] = (anchor_ms - high_time) / 1000
            if len(recent) >= 10:
                result["time_at_high"] = float((recent["close"] >= recent["high"].rolling(10, min_periods=1).max() * 0.995).mean())
                high_flags = recent["high"] >= recent["high"].rolling(15, min_periods=3).max()
                result["new_high_rate"] = float(high_flags.tail(15).sum()) / max(1, len(high_flags.tail(15)))
                half = max(3, len(high_flags) // 2)
                result["high_progression_decay"] = float(high_flags.tail(half).sum()) / max(1, float(high_flags.head(half).sum()))
    if not five_sec.empty:
        near_end = five_sec[
            (five_sec["open_ms"] >= anchor_ms - 60_000)
            & (five_sec["close_ms"] + 1 <= anchor_ms)
        ]
        if len(near_end) >= 2:
            result["intrabar_range_share"] = _safe_float(float(near_end["high"].sub(near_end["low"]).max()) / max(float(near_end["high"].sub(near_end["low"]).sum()), 1e-12))
    result["open_interest"] = _asof_value(metrics, anchor_ms, "sum_open_interest")
    result["open_interest_value"] = _asof_value(metrics, anchor_ms, "sum_open_interest_value")
    result["long_short_ratio"] = _asof_value(metrics, anchor_ms, "count_long_short_ratio")
    result["toptrader_long_short_ratio"] = _asof_value(metrics, anchor_ms, "count_toptrader_long_short_ratio")
    result["taker_long_short_vol_ratio"] = _asof_value(metrics, anchor_ms, "sum_taker_long_short_vol_ratio")
    return result


def _anchor_rows(event: dict[str, Any], frames: dict[str, pd.DataFrame], metrics: pd.DataFrame, benchmark: dict[str, pd.DataFrame]) -> list[dict[str, Any]]:
    anchors = [("start", event["start_ms"]), ("end", event["end_ms"])]
    if event.get("high_ms") is not None and pd.notna(event.get("high_ms")):
        anchors.append(("high", int(event["high_ms"])))
    if event.get("low_ms") is not None and pd.notna(event.get("low_ms")):
        anchors.append(("low", int(event["low_ms"])))
    event_meta = dict(event)
    rows: list[dict[str, Any]] = []
    for anchor_role, anchor_ms in anchors:
        row = dict(event_meta)
        row.update({"anchor_role": anchor_role, "anchor_ms": int(anchor_ms), "formula_version": FORMULA_VERSION})
        scalar = _event_scalar_features(event, frames, metrics, int(anchor_ms))
        row.update({f"event_{key}": value for key, value in scalar.items()})
        for timeframe, frame in frames.items():
            if frame.empty:
                continue
            visible = frame[frame["close_ms"] + 1 <= int(anchor_ms)]
            if visible.empty:
                continue
            sample = visible.iloc[-1]
            prefix = timeframe.replace("/", "_") + "_"
            row[f"{prefix}sample_open_ms"] = int(sample["open_ms"])
            row[f"{prefix}sample_close_ms"] = int(sample["close_ms"])
            for column in frame.columns:
                if column in {"open_ms", "close_ms", "complete_bar"}:
                    continue
                value = sample[column]
                if isinstance(value, (np.bool_, bool)):
                    value = bool(value)
                elif isinstance(value, (np.integer, int)):
                    value = int(value)
                elif isinstance(value, (np.floating, float)):
                    value = _safe_float(value)
                row[f"{prefix}{column}"] = value
            benchmark_frame = benchmark.get(timeframe)
            if benchmark_frame is not None and not benchmark_frame.empty:
                b_visible = benchmark_frame[benchmark_frame["close_ms"] + 1 <= int(anchor_ms)]
                if not b_visible.empty:
                    for window_minutes in (15, 60):
                        cutoff = int(anchor_ms) - window_minutes * MS_1M
                        asset_past = visible[visible["close_ms"] + 1 <= cutoff]
                        benchmark_past = b_visible[b_visible["close_ms"] + 1 <= cutoff]
                        if asset_past.empty or benchmark_past.empty:
                            continue
                        asset_return = float(sample["close"]) / max(float(asset_past.iloc[-1]["close"]), 1e-12) - 1
                        benchmark_return = float(b_visible.iloc[-1]["close"]) / max(float(benchmark_past.iloc[-1]["close"]), 1e-12) - 1
                        row[f"{prefix}relative_return_btc_{window_minutes}m"] = _safe_float(asset_return - benchmark_return)
        rows.append(row)
    return rows


def _targets(event: dict[str, Any], one_sec: pd.DataFrame) -> dict[str, Any]:
    result = {"event_id": event["event_id"], "symbol": event["symbol"], "direction": event.get("direction"), "end_ms": int(event["end_ms"]), "price_source": "1s_ohlcv"}
    if one_sec.empty:
        return result
    base = float(one_sec[one_sec["open_ms"] >= int(event["end_ms"])].iloc[0]["close"]) if not one_sec[one_sec["open_ms"] >= int(event["end_ms"])].empty else None
    if base is None or base <= 0:
        return result
    future = one_sec[one_sec["open_ms"] >= int(event["end_ms"])].copy()
    for label, seconds in (("30s", 30), ("1m", 60), ("3m", 180), ("5m", 300), ("10m", 600), ("15m", 900), ("30m", 1800)):
        part = future[future["open_ms"] < int(event["end_ms"]) + seconds * 1000]
        if part.empty:
            continue
        result[f"ret_after_{label}"] = _safe_float(float(part.iloc[-1]["close"]) / base - 1)
        result[f"fwd_max_{label}"] = _safe_float(float(part["high"].max()) / base - 1)
        result[f"fwd_min_{label}"] = _safe_float(float(part["low"].min()) / base - 1)
    result["mae_short"] = _safe_float(float(future["high"].max()) / base - 1) if not future.empty else None
    result["mfe_short"] = _safe_float(float(future["low"].min()) / base - 1) if not future.empty else None
    if not future.empty:
        result["mae_time_ms"] = int(future.loc[future["high"].idxmax(), "open_ms"])
        result["mfe_time_ms"] = int(future.loc[future["low"].idxmin(), "open_ms"])
    return result


def _coverage(event: dict[str, Any], source: str, timeframe: str, frame: pd.DataFrame, start_ms: int, end_ms: int) -> dict[str, Any]:
    expected_step = TIMEFRAME_MS.get(timeframe)
    if not frame.empty and {"open_ms", "close_ms"}.issubset(frame.columns):
        frame = frame[(frame["open_ms"] < end_ms) & (frame["close_ms"] >= start_ms)]
    observed = int(len(frame))
    gaps = np.diff(frame["open_ms"].to_numpy(np.int64)) if observed > 1 else np.array([], dtype=np.int64)
    gap_values = gaps[gaps > expected_step] if expected_step else np.array([], dtype=np.int64)
    first = int(frame["open_ms"].min()) if observed else None
    last = int(frame["close_ms"].max()) if observed else None
    expected = max(0, math.ceil((end_ms - start_ms) / expected_step)) if expected_step else None
    status = "missing" if observed == 0 else "complete"
    if observed and (first > start_ms or (last is not None and last < end_ms - 1) or len(gap_values)):
        status = "partial"
    return {
        "event_id": event["event_id"],
        "symbol": event["symbol"],
        "source": source,
        "timeframe": timeframe,
        "requested_start_ms": start_ms,
        "requested_end_ms": end_ms,
        "expected_bars": expected,
        "observed_bars": observed,
        "first_open_ms": first,
        "last_close_ms": last,
        "gap_count": int(len(gap_values)),
        "max_gap_ms": int(gap_values.max()) if len(gap_values) else 0,
        "status": status,
        "reason": None if status == "complete" else "archive coverage or continuity gap",
    }


def _write_frame(frame: pd.DataFrame, path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    if frame.empty:
        return 0
    frame.to_parquet(path, index=False, compression="zstd")
    return len(frame)


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value)


def _worker(task: dict[str, Any]) -> dict[str, Any]:
    symbol = str(task["symbol"])
    output_dir = Path(task["output_dir"])
    shard = _safe_name(symbol)
    started = time.monotonic()
    try:
        connection = duckdb.connect(":memory:")
        connection.execute("SET TimeZone='UTC'")
        connection.execute(f"SET threads={int(task['duckdb_threads'])}")
        frames: dict[str, pd.DataFrame] = {}
        event_start = min(int(e["start_ms"]) for e in task["events"])
        event_end = max(int(e["end_ms"]) for e in task["events"])
        bg_end = event_end + POST_MS
        for timeframe in NATIVE_TIMEFRAMES:
            raw = _query_candle_intervals(
                connection,
                task["candle_paths"].get(timeframe, []),
                symbol,
                timeframe,
                task["background_intervals"],
            )
            frames[timeframe] = _add_bar_features(raw, timeframe)
        raw_1s = _query_candle_intervals(
            connection,
            task["candle_paths"].get("1s", []),
            symbol,
            "1s",
            task["micro_intervals"],
        )
        frames["1s"] = _add_bar_features(raw_1s, "1s")
        frames["5s"] = _add_bar_features(_aggregate_5s(raw_1s), "5s")
        metrics = _query_metric_intervals(
            connection,
            task["metric_paths"],
            symbol,
            task["metrics_intervals"],
        )
        for timeframe in ALL_BAR_TIMEFRAMES:
            if timeframe in frames:
                frames[timeframe] = _merge_metrics(frames[timeframe], metrics)
        benchmark: dict[str, pd.DataFrame] = {}
        benchmark_symbol = task.get("benchmark_symbol")
        if benchmark_symbol and task.get("benchmark_paths"):
            benchmark_raw = _query_candle_intervals(
                connection,
                task["benchmark_paths"].get("1m", []),
                benchmark_symbol,
                "1m",
                task["background_intervals"],
            )
            benchmark["1m"] = _add_bar_features(benchmark_raw, "1m")
            benchmark["5m"] = _add_bar_features(_aggregate_frame(benchmark_raw, "5m", "1m"), "5m")
            benchmark["15m"] = _add_bar_features(_aggregate_frame(benchmark_raw, "15m", "1m"), "15m")
        connection.close()

        bar_paths: dict[str, Path] = {}
        bar_counts: dict[str, int] = {}
        for timeframe, frame in frames.items():
            if frame.empty:
                continue
            persist_start = event_start - MICRO_PRE_MS if timeframe in {"1s", "5s"} else event_start
            persisted = frame[(frame["open_ms"] >= persist_start) & (frame["open_ms"] < bg_end)].copy()
            persisted.insert(0, "symbol", symbol)
            persisted.insert(1, "timeframe", timeframe)
            path = output_dir / "bars" / timeframe / f"part-{shard}.parquet"
            bar_paths[timeframe] = path
            bar_counts[timeframe] = _write_frame(persisted, path)

        event_rows: list[dict[str, Any]] = []
        target_rows: list[dict[str, Any]] = []
        availability_rows: list[dict[str, Any]] = []
        for event in task["events"]:
            event_id = event["event_id"]
            for timeframe in NATIVE_TIMEFRAMES:
                availability_rows.append(_coverage(event, "candles", timeframe, frames.get(timeframe, pd.DataFrame()), int(event["start_ms"]) - BACKGROUND_PRE_MS, int(event["end_ms"]) + POST_MS))
            availability_rows.append(_coverage(event, "candles", "1s", frames.get("1s", pd.DataFrame()), int(event["start_ms"]) - MICRO_PRE_MS, int(event["end_ms"]) + POST_MS))
            metric_coverage = metrics.copy()
            if not metric_coverage.empty:
                metric_coverage["open_ms"] = metric_coverage["available_ms"]
                metric_coverage["close_ms"] = metric_coverage["available_ms"]
            availability_rows.append(_coverage(event, "metrics", "5m", metric_coverage, int(event["start_ms"]) - METRICS_PRE_MS, int(event["end_ms"]) + POST_MS))
            event_frames = {key: value for key, value in frames.items() if key in ALL_BAR_TIMEFRAMES}
            event_rows.extend(_anchor_rows(event, event_frames, metrics, benchmark))
            target_rows.append(_targets(event, frames.get("1s", pd.DataFrame())))

        derivative_path = output_dir / "derivatives" / f"part-{shard}.parquet"
        metric_counts = _write_frame(metrics.assign(symbol=symbol) if not metrics.empty else metrics, derivative_path)
        event_path = output_dir / "event_features" / f"part-{shard}.parquet"
        target_path = output_dir / "targets" / f"part-{shard}.parquet"
        availability_path = output_dir / "availability" / f"part-{shard}.parquet"
        event_count = _write_frame(pd.DataFrame(event_rows), event_path)
        target_count = _write_frame(pd.DataFrame(target_rows), target_path)
        availability_count = _write_frame(pd.DataFrame(availability_rows), availability_path)
        return {
            "symbol": symbol,
            "status": "ok",
            "events": len(task["events"]),
            "event_feature_rows": event_count,
            "target_rows": target_count,
            "availability_rows": availability_count,
            "bar_counts": bar_counts,
            "metric_rows": metric_counts,
            "seconds": round(time.monotonic() - started, 2),
        }
    except Exception as exc:  # noqa: BLE001
        failure = pd.DataFrame([{
            "symbol": symbol,
            "status": "failed",
            "error_type": type(exc).__name__,
            "error": repr(exc),
        }])
        _write_frame(failure, output_dir / "failures" / f"part-{shard}.parquet")
        return {
            "symbol": symbol,
            "status": "failed",
            "events": len(task.get("events", [])),
            "error_type": type(exc).__name__,
            "error": repr(exc),
            "seconds": round(time.monotonic() - started, 2),
        }


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_feature_dictionary(output_dir: Path, metrics_available: bool, benchmark_available: bool) -> int:
    rows = []
    for spec in FEATURE_SPECS:
        row = dict(spec)
        if spec["source_requirements"] == "metrics sidecar" and not metrics_available:
            row["status"] = "missing_source"
            row["nullable_reason"] = "metrics index not available"
        if spec["feature_name"] == "relative_return_btc" and not benchmark_available:
            row["status"] = "missing_source"
            row["nullable_reason"] = "benchmark symbol or candle coverage missing"
        rows.append(row)
    return _write_frame(pd.DataFrame(rows), output_dir / "feature_dictionary.parquet")


def _default_output_dir() -> Path:
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    return Path("reports/amplitude/indicator_features") / f"run_id={stamp}"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", nargs="+", type=Path, default=[
        Path("reports/amplitude/daily_amplitude_cycles_spike_up.csv"),
        Path("reports/amplitude/daily_amplitude_cycles_spike_down.csv"),
    ])
    parser.add_argument("--archive-index", type=Path, default=Path("data/market/candles") / ARCHIVE_INDEX_FILENAME)
    parser.add_argument("--metrics-index", type=Path, default=Path("data/market/metrics") / METRICS_INDEX_FILENAME)
    parser.add_argument("--out-dir", type=Path, default=None)
    parser.add_argument("--benchmark-symbol", default="BTCUSDT")
    parser.add_argument("--workers", type=int, default=13)
    parser.add_argument("--duckdb-threads", type=int, default=1)
    parser.add_argument("--limit-events", type=int, default=0)
    parser.add_argument("--limit-symbols", type=int, default=0)
    parser.add_argument("--skip-index-verify", action="store_true")
    parser.add_argument("--strict", action="store_true", help="任一 symbol 失败时返回非零")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.workers <= 0 or args.duckdb_threads <= 0:
        raise ValueError("workers and duckdb-threads must be positive")
    events = _parse_events([path.resolve() for path in args.events])
    if args.limit_events > 0:
        events = events.head(args.limit_events).copy()
    if args.limit_symbols > 0:
        symbols = sorted(events["symbol"].unique())[: args.limit_symbols]
        events = events[events["symbol"].isin(symbols)].copy()
    if events.empty:
        raise ValueError("no valid events")

    archive_index_path = args.archive_index.resolve()
    archive_root = archive_index_path.parent
    archive_index = load_archive_index(
        archive_index_path,
        verify_files=not args.skip_index_verify,
    )
    metrics_available = args.metrics_index.resolve().is_file()
    metrics_index = (
        load_metrics_index(
            args.metrics_index.resolve(),
            verify_files=not args.skip_index_verify,
        ).to_pandas()
        if metrics_available
        else pd.DataFrame()
    )
    benchmark_symbol = str(args.benchmark_symbol).strip().upper() or None
    benchmark_available = benchmark_symbol is not None and not archive_index[
        (archive_index["symbol"] == benchmark_symbol) & (archive_index["timeframe"] == "1m")
    ].empty
    out_dir = (args.out_dir or _default_output_dir()).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_feature_dictionary(out_dir, metrics_available, benchmark_available)

    event_output = events.copy()
    for column in ("start_ms", "end_ms", "high_ms", "low_ms"):
        event_output[column] = pd.to_numeric(event_output[column], errors="coerce").astype("Int64")
    _write_frame(event_output, out_dir / "events.parquet")

    tasks: list[dict[str, Any]] = []
    for symbol, group in events.groupby("symbol", sort=True):
        event_dicts = group.to_dict(orient="records")
        background_intervals = _merge_intervals(
            (int(row["start_ms"]) - BACKGROUND_PRE_MS, int(row["end_ms"]) + POST_MS)
            for row in event_dicts
        )
        micro_intervals = _merge_intervals(
            (int(row["start_ms"]) - MICRO_PRE_MS, int(row["end_ms"]) + POST_MS)
            for row in event_dicts
        )
        metrics_intervals = _merge_intervals(
            (int(row["start_ms"]) - METRICS_PRE_MS, int(row["end_ms"]) + POST_MS)
            for row in event_dicts
        )
        candle_paths = {
            timeframe: _select_paths_for_intervals(
                archive_index,
                archive_root,
                symbol,
                timeframe,
                micro_intervals if timeframe == "1s" else background_intervals,
            )
            for timeframe in (*NATIVE_TIMEFRAMES, "1s")
        }
        metric_paths = _select_metric_paths_for_intervals(
            metrics_index,
            args.metrics_index.resolve().parent,
            symbol,
            metrics_intervals,
        ) if metrics_available else []
        benchmark_paths = {}
        if benchmark_available and benchmark_symbol:
            benchmark_paths["1m"] = _select_paths_for_intervals(
                archive_index,
                archive_root,
                benchmark_symbol,
                "1m",
                background_intervals,
            )
        selected_frames = []
        for paths in candle_paths.values():
            selected_frames.extend(paths)
        for paths in benchmark_paths.values():
            selected_frames.extend(paths)
        selected = archive_index[archive_index["relative_path"].isin([str(Path(path).relative_to(archive_root)) for path in selected_frames])]
        if not args.skip_index_verify and not selected.empty:
            verify_archive_index_files(selected, archive_root)
        tasks.append({
            "symbol": symbol,
            "events": event_dicts,
            "background_intervals": background_intervals,
            "micro_intervals": micro_intervals,
            "metrics_intervals": metrics_intervals,
            "candle_paths": candle_paths,
            "metric_paths": metric_paths,
            "benchmark_paths": benchmark_paths,
            "benchmark_symbol": benchmark_symbol,
            "output_dir": str(out_dir),
            "duckdb_threads": args.duckdb_threads,
        })

    effective_workers = min(args.workers, len(tasks))
    manifest = {
        "formula_version": FORMULA_VERSION,
        "created_at_utc": dt.datetime.now(dt.UTC).isoformat(),
        "events": [
            {"path": str(path.resolve()), "sha256": _file_sha256(path)}
            for path in args.events
        ],
        "archive_index": str(archive_index_path),
        "archive_index_rows": int(len(archive_index)),
        "metrics_index": str(args.metrics_index.resolve()) if metrics_available else None,
        "metrics_index_rows": int(len(metrics_index)) if metrics_available else 0,
        "benchmark_symbol": benchmark_symbol,
        "workers_requested": args.workers,
        "workers": effective_workers,
        "duckdb_threads_per_worker": args.duckdb_threads,
        "event_count": int(len(events)),
        "symbol_count": int(len(tasks)),
        "outputs": {
            "events": "events.parquet",
            "feature_dictionary": "feature_dictionary.parquet",
            "event_features": "event_features/*.parquet",
            "bars": "bars/{1s,5s,1m,5m,15m,1h}/*.parquet",
            "derivatives": "derivatives/*.parquet",
            "availability": "availability/*.parquet",
            "targets": "targets/*.parquet",
            "failures": "failures/*.parquet",
        },
    }
    pool_context = multiprocessing.get_context("spawn")
    results: list[dict[str, Any]] = []
    print(
        f"启动: events={len(events)} symbols={len(tasks)} "
        f"workers_requested={args.workers} workers_effective={effective_workers}",
        flush=True,
    )
    with pool_context.Pool(processes=effective_workers) as pool:
        for result in pool.imap_unordered(_worker, tasks, chunksize=1):
            results.append(result)
            print(f"{result['symbol']}: {result['status']} events={result.get('events', 0)}", flush=True)
    results.sort(key=lambda item: item["symbol"])
    manifest["results"] = results
    manifest["ok_symbols"] = sum(item["status"] == "ok" for item in results)
    manifest["failed_symbols"] = sum(item["status"] != "ok" for item in results)
    (out_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    print(f"完成: events={len(events)} symbols={len(tasks)} ok={manifest['ok_symbols']} failed={manifest['failed_symbols']} output={out_dir}")
    return 2 if args.strict and manifest["failed_symbols"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
