"""主流量化指标计算库（研究用）。

以 1m OHLCV 为输入，聚合 5m/15m 后计算常用技术指标。
所有指标在"当前 bar"（信号/异动时刻）取值。
"""
from __future__ import annotations

import math
from typing import Iterable, Sequence

import numpy as np


# ---------- 基础聚合 ----------

def aggregate_ohlcv(
    opens: Sequence[float],
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    volumes: Sequence[float],
    times_ms: Sequence[int],
    tf_ms: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """把 1m 数据聚合到指定周期（tf_ms=5m/15m/1h）。

    返回 (open, high, low, close, volume, bar_open_time_ms)，按时间升序。
    """
    o, h, l, c, v, t = [], [], [], [], [], []
    for i in range(len(times_ms)):
        bar = (times_ms[i] // tf_ms) * tf_ms
        if t and bar == t[-1]:
            h[-1] = max(h[-1], highs[i])
            l[-1] = min(l[-1], lows[i])
            c[-1] = closes[i]
            v[-1] += volumes[i]
        else:
            o.append(opens[i])
            h.append(highs[i])
            l.append(lows[i])
            c.append(closes[i])
            v.append(volumes[i])
            t.append(bar)
    return (np.array(o), np.array(h), np.array(l), np.array(c), np.array(v), np.array(t))


# ---------- 技术指标（向量化，返回数组） ----------

def ema(values: np.ndarray, span: int) -> np.ndarray:
    out = np.empty(len(values))
    if len(values) == 0:
        return out
    out[0] = values[0]
    alpha = 2.0 / (span + 1)
    for i in range(1, len(values)):
        out[i] = alpha * values[i] + (1 - alpha) * out[i - 1]
    return out


def sma(values: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(values), np.nan)
    if len(values) < n:
        return out
    cs = np.cumsum(np.insert(values, 0, 0.0))
    out[n - 1:] = (cs[n:] - cs[:-n]) / n
    return out


def rsi(values: np.ndarray, period: int = 14) -> np.ndarray:
    out = np.full(len(values), np.nan)
    if len(values) <= period:
        return out
    delta = np.diff(values)
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    avg_gain = np.full(len(values), np.nan)
    avg_loss = np.full(len(values), np.nan)
    avg_gain[period] = np.mean(gain[:period])
    avg_loss[period] = np.mean(loss[:period])
    for i in range(period + 1, len(values)):
        avg_gain[i] = (avg_gain[i - 1] * (period - 1) + gain[i - 1]) / period
        avg_loss[i] = (avg_loss[i - 1] * (period - 1) + loss[i - 1]) / period
    rs = avg_gain / np.where(avg_loss == 0, 1e-12, avg_loss)
    out[period:] = 100.0 - 100.0 / (1.0 + rs[period:])
    return out


def macd(values: np.ndarray, fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    ema_f = ema(values, fast)
    ema_s = ema(values, slow)
    macd_line = ema_f - ema_s
    signal_line = ema(macd_line, signal)
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def roc(values: np.ndarray, n: int = 5) -> np.ndarray:
    out = np.full(len(values), np.nan)
    if len(values) > n:
        out[n:] = (values[n:] / np.where(values[:-n] == 0, 1e-12, values[:-n]) - 1.0) * 100.0
    return out


def atr(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int = 14) -> np.ndarray:
    out = np.full(len(closes), np.nan)
    if len(closes) <= period:
        return out
    prev_close = np.roll(closes, 1)
    prev_close[0] = closes[0]
    tr = np.maximum(highs - lows, np.maximum(np.abs(highs - prev_close), np.abs(lows - prev_close)))
    out[period] = np.mean(tr[1:period + 1])
    for i in range(period + 1, len(closes)):
        out[i] = (out[i - 1] * (period - 1) + tr[i]) / period
    return out


def bb_width(closes: np.ndarray, n: int = 20, k: float = 2.0) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    mid = sma(closes, n)
    out_mid = np.full(len(closes), np.nan)
    out_std = np.full(len(closes), np.nan)
    out_up = np.full(len(closes), np.nan)
    out_dn = np.full(len(closes), np.nan)
    for i in range(n - 1, len(closes)):
        seg = closes[i - n + 1:i + 1]
        m = np.mean(seg)
        sd = np.std(seg)
        out_mid[i] = m
        out_std[i] = sd
        out_up[i] = m + k * sd
        out_dn[i] = m - k * sd
    width = (out_up - out_dn) / np.where(out_mid == 0, 1e-12, out_mid)
    return out_up, out_mid, out_dn, width


def linear_slope_r2(values: np.ndarray) -> tuple[float, float]:
    """log(values) 对等间距索引的线性回归，返回 (slope_bps, r2)。"""
    n = len(values)
    if n < 3:
        return 0.0, 0.0
    lg = np.log(np.maximum(values, 1e-12))
    xs = np.arange(n, dtype=float)
    xm, ym = xs.mean(), lg.mean()
    sxy = np.sum((xs - xm) * (lg - ym))
    sxx = np.sum((xs - xm) ** 2)
    if sxx == 0:
        return 0.0, 0.0
    slope = sxy / sxx
    icpt = ym - slope * xm
    pred = icpt + slope * xs
    ssr = np.sum((lg - pred) ** 2)
    sst = np.sum((lg - ym) ** 2)
    r2 = 1.0 - ssr / sst if sst > 0 else 0.0
    return slope * 10000.0, r2


def percentile(vals: Sequence[float], p: float) -> float:
    arr = np.sort(np.asarray(vals, dtype=float))
    if len(arr) == 0:
        return float("nan")
    idx = (len(arr) - 1) * p
    lo = int(math.floor(idx))
    hi = min(lo + 1, len(arr) - 1)
    frac = idx - lo
    return float(arr[lo] * (1 - frac) + arr[hi] * frac)


def winsorized_mean(vals: Sequence[float], pct: float = 0.05) -> float:
    arr = np.asarray(vals, dtype=float)
    if len(arr) == 0:
        return float("nan")
    lo = float(np.percentile(arr, pct * 100))
    hi = float(np.percentile(arr, (1 - pct) * 100))
    clipped = np.clip(arr, lo, hi)
    return float(np.mean(clipped))


def parkinson_vol(highs: Sequence[float], lows: Sequence[float], annualize: bool = False) -> float:
    n = len(highs)
    if n < 2:
        return 0.0
    hl = np.log(np.maximum(np.asarray(highs), 1e-12) / np.maximum(np.asarray(lows), 1e-12))
    var = np.mean(hl ** 2) / (4.0 * math.log(2.0))
    sd = math.sqrt(var)
    return sd * math.sqrt(252 * 24 * 12) if annualize else sd


def real_vol(log_returns: np.ndarray, periods_per_year: float | None = None) -> float:
    """对数收益的标准差；periods_per_year 给定则年化。"""
    if len(log_returns) < 2:
        return 0.0
    sd = float(np.std(log_returns, ddof=1))
    return sd * math.sqrt(periods_per_year) if periods_per_year else sd


def obv_slope(closes: np.ndarray, volumes: np.ndarray, lookback: int = 20) -> float:
    if len(closes) < lookback + 1:
        return 0.0
    obv = np.zeros(len(closes))
    for i in range(1, len(closes)):
        if closes[i] > closes[i - 1]:
            obv[i] = obv[i - 1] + volumes[i]
        elif closes[i] < closes[i - 1]:
            obv[i] = obv[i - 1] - volumes[i]
        else:
            obv[i] = obv[i - 1]
    seg = obv[-lookback:]
    xs = np.arange(lookback, dtype=float)
    slope = np.polyfit(xs, seg, 1)[0]
    return float(slope)


def vwap_dev(
    o: np.ndarray, h: np.ndarray, l: np.ndarray, c: np.ndarray, v: np.ndarray, n: int = 20
) -> float:
    """最近 n 根的 VWAP 偏离%。"""
    seg = slice(-n, None)
    typical = (o[seg] + h[seg] + l[seg] + c[seg]) / 4.0
    vseg = v[seg]
    denom = np.sum(vseg)
    if denom == 0:
        return 0.0
    vwap = np.sum(typical * vseg) / denom
    last = c[-1]
    return float((last / vwap - 1.0) * 100.0)


def cci(h: np.ndarray, l: np.ndarray, c: np.ndarray, n: int = 20) -> float:
    if len(c) < n:
        return 0.0
    tp = (h[-n:] + l[-n:] + c[-n:]) / 3.0
    m = np.mean(tp)
    mad = np.mean(np.abs(tp - m))
    if mad == 0:
        return 0.0
    return float((tp[-1] - m) / (0.015 * mad))


def stochastic(closes: np.ndarray, k: int = 14, d: int = 3) -> tuple[float, float]:
    if len(closes) < k:
        return 0.0, 0.0
    seg = closes[-k:]
    hh, ll = float(np.max(seg)), float(np.min(seg))
    k_val = (closes[-1] - ll) / (hh - ll) * 100.0 if hh > ll else 50.0
    return float(k_val), float(k_val)  # D 简化为 K（研究阶段够用）