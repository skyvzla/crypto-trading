"""真实 spike 信号短周期指标研究。

目标：策略持仓中位 15 分钟，因此用"信号后 5/10/15/30 分钟走势"作为目标，
重新评估各指标对短期方向的区分度（替代之前 4h 事后口径）。

输入: reports/spike-v2.2-noreject-smoke/runs/*/trades.csv (真实 69 笔信号)
输出: reports/spike_signal_shortterm_metrics.csv
读取: 按 symbol 分块，一次读入该币全部信号所需跨度。
"""
from __future__ import annotations

import datetime as dt
import glob
import os
import sys

import duckdb
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))
from trading_platform.research.indicators import (  # noqa: E402
    aggregate_ohlcv,
    atr,
    bb_width,
    cci,
    ema,
    linear_slope_r2,
    macd,
    obv_slope,
    parkinson_vol,
    percentile,
    roc,
    rsi,
    stochastic,
    vwap_dev,
    winsorized_mean,
)

MS_MIN = 60_000
MS_5M = 5 * MS_MIN
MS_15M = 15 * MS_MIN
MS_1H = 60 * MS_MIN
MS_DAY = 24 * MS_1H

BOX_LOOKBACK = 3 * MS_DAY
AFTER_WINDOW = 30 * MS_MIN
PRE_BUFFER = 1 * MS_DAY


def parse_ms(iso: str) -> int:
    return int(pd.Timestamp(iso).timestamp() * 1000)


class SymbolReader:
    """按 symbol 分块读入 1m 数据（覆盖该币全部信号跨度）。"""

    def __init__(self, root: str, index: pd.DataFrame, con: duckdb.DuckDBPyConnection):
        self.root = root
        self.con = con
        self.idx1m = index[index["timeframe"] == "1m"]
        self.cache: dict[str, tuple[int, int, dict[int, tuple]]] = {}

    def read_symbol(self, symbol: str, intervals: list[tuple[int, int]]) -> None:
        merged = []
        for t0, t1 in sorted(intervals):
            if merged and t0 <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], t1))
            else:
                merged.append((t0, t1))
        by_time: dict[int, tuple] = {}
        for t0, t1 in merged:
            files = [
                os.path.join(self.root, v)
                for v in self.idx1m[
                    (self.idx1m["symbol"] == symbol)
                    & (self.idx1m["first_open_ms"] < t1)
                    & (self.idx1m["last_close_ms"] >= t0)
                ]["relative_path"]
            ]
            if not files:
                continue
            rows = self.con.execute(
                """SELECT epoch_ms(open_time),open,high,low,close,volume
                   FROM read_parquet(?, union_by_name=true)
                   WHERE symbol=? AND timeframe='1m'
                     AND epoch_ms(open_time)>=? AND epoch_ms(open_time)<?
                   ORDER BY open_time""",
                (files, symbol, t0, t1),
            ).fetchall()
            for r in rows:
                by_time[r[0]] = tuple(r[1:])
        self.cache[symbol] = (min(by_time) if by_time else 0, max(by_time) if by_time else 0, by_time)

    def slice(self, symbol: str, t0: int, t1: int) -> list[tuple]:
        if symbol not in self.cache:
            return []
        lo, hi, by = self.cache[symbol]
        if t0 < lo or t1 > hi:
            return []
        times = sorted(k for k in by if t0 <= k < t1)
        return [(k,) + by[k] for k in times]


def ohlcv_rows(rows: list[tuple]):
    if not rows:
        return (np.array([]),) * 6
    a = np.array(rows)
    return a[:, 0].astype(np.int64), a[:, 1], a[:, 2], a[:, 3], a[:, 4], a[:, 5]


def compute_box(highs: np.ndarray, lows: np.ndarray) -> dict:
    if len(highs) < 8:
        return {}
    slope_bps, r2 = linear_slope_r2(highs)
    out = {"slope_bps": round(slope_bps, 2), "r2": round(r2, 3)}
    for name, p in (("p90", 0.90), ("p95", 0.95), ("p99", 0.99)):
        out[f"up_{name}"] = round(percentile(highs, p), 8)
    out["up_win"] = round(winsorized_mean(highs), 8)
    lg = np.log(np.maximum(highs, 1e-12))
    xs = np.arange(len(highs), dtype=float)
    slope = np.polyfit(xs, lg, 1)[0]
    icpt = np.mean(lg) - slope * np.mean(xs)
    resid = lg - (icpt + slope * xs)
    sigma = float(np.std(resid))
    latest = icpt + slope * (len(highs) - 1)
    out["up_reg15"] = round(float(np.exp(latest + 1.5 * sigma)), 8)
    out["dn_p10"] = round(percentile(lows, 0.10), 8)
    out["dn_win"] = round(winsorized_mean(lows), 8)
    out["dn_reg15"] = round(float(np.exp(latest - 1.5 * sigma)), 8)
    return out


def compute_signal_metrics(reader: SymbolReader, symbol: str, sig_ms: int, trig: float) -> dict:
    """对单个真实信号计算完整指标 + 短周期后走势。"""
    res: dict = {}
    minute_start = (sig_ms // MS_MIN) * MS_MIN
    cur_hour = minute_start - (minute_start % MS_1H)

    # ---- 1. 箱体（信号前 3d，1h 粒度） ----
    box_t0 = cur_hour - BOX_LOOKBACK
    rows = reader.slice(symbol, box_t0, cur_hour)
    if len(rows) >= 24 * 6:
        t, o, h, l, c, v = ohlcv_rows(rows)
        ho, hh, hl, hc, hv, ht = aggregate_ohlcv(o, h, l, c, v, t, MS_1H)
        box = compute_box(hh, hl)
        res.update({f"box_{k}": v for k, v in box.items()})

    # ---- 2. 信号前 30 分钟区间指标（5m/15m） ----
    rows = reader.slice(symbol, sig_ms - 30 * MS_MIN, sig_ms)
    if len(rows) >= 30:
        t, o, h, l, c, v = ohlcv_rows(rows)
        o5, h5, l5, c5, v5, t5 = aggregate_ohlcv(o, h, l, c, v, t, MS_5M)
        o15, h15, l15, c15, v15, t15 = aggregate_ohlcv(o, h, l, c, v, t, MS_15M)

        if len(c5) >= 6:
            res["rsi_5m"] = round(float(rsi(c5, 14)[-1]), 1)
            _, _, mh = macd(c5, 12, 26, 9)
            res["macd_hist_5m"] = round(float(mh[-1]), 8)
            res["roc_5m"] = round(float(roc(c5, 5)[-1]), 3)
            res["cci_5m"] = round(cci(h5, l5, c5, 20), 1)
            k, _ = stochastic(c5, 14)
            res["sto_k_5m"] = round(k, 1)
            e20 = ema(c5, 20)[-1]
            res["ema_ratio_5m"] = round(float(c5[-1] / e20), 4) if e20 > 0 else float("nan")
            res["bb_width_5m"] = round(float(bb_width(c5, 20, 2.0)[3][-1]), 4)
            a5 = atr(h5, l5, c5, 14)[-1]
            res["atr_ratio_5m"] = round(float(a5 / c5[-1]), 5) if c5[-1] > 0 else float("nan")
            res["obv_slope_5m"] = round(obv_slope(c5, v5, 20), 2)
            res["vwap_dev_5m"] = round(vwap_dev(o5, h5, l5, c5, v5, 20), 2)

        if len(c15) >= 6:
            res["rsi_15m"] = round(float(rsi(c15, 14)[-1]), 1)
            res["roc_15m"] = round(float(roc(c15, 5)[-1]), 3)

        # 触发时刻特征
        avg30 = float(np.mean(c[-30:]))
        res["dev30_pct"] = round(float(trig / avg30 - 1.0) * 100.0, 2)
        highs = [r[3] for r in rows]
        lows = [r[4] for r in rows]
        res["rng60_pct"] = round(float((max(highs) - min(lows)) / min(lows) * 100.0), 2)
        # 拉升形态
        if len(c) >= 11:
            ret5 = (c[-1] / c[-6] - 1)
            ret_prev = (c[-6] / c[-11] - 1)
            res["accel_5m"] = round(float(ret5 - ret_prev), 4)
        res["pulse_1m"] = round(float(np.max(np.diff(c[-30:]) / np.maximum(c[-30:-1], 1e-12))), 5)
        n = 0
        for i in range(len(c) - 1, 0, -1):
            if c[i] > c[i - 1]:
                n += 1
            else:
                break
        res["consecutive_green"] = n

    # ---- 3. 信号后 5/10/15/30 分钟走势（空头视角：价格上涨=亏损） ----
    rows = reader.slice(symbol, sig_ms, sig_ms + AFTER_WINDOW)
    if len(rows) >= 6:
        times = [r[0] for r in rows]
        highs = [r[3] for r in rows]
        base = trig
        for label, mins in (("5m", 5), ("10m", 10), ("15m", 15), ("30m", 30)):
            target = sig_ms + mins * MS_MIN
            idx = next((i for i, tt in enumerate(times) if tt >= target), len(times) - 1)
            ref = highs[idx]  # 空头看最高价（不利方向）
            res[f"fwd_high_{label}"] = round(float(ref / base - 1.0) * 100.0, 2)
        res["fwd_max_15m"] = round(float(max(highs[:16]) / base - 1.0) * 100.0, 2)
        res["fwd_max_30m"] = round(float(max(highs) / base - 1.0) * 100.0, 2)
    return res


def main() -> None:
    trades = []
    for f in sorted(glob.glob("reports/spike-v2.2-noreject-smoke/runs/*/trades.csv")):
        df = pd.read_csv(f)
        for t in df.itertuples():
            trades.append(
                {
                    "symbol": t.symbol,
                    "signal_time": int(float(t.signal_time)),
                    "trigger_price": float(t.trigger_price),
                    "net_pnl": float(t.net_pnl),
                    "exit_reason": t.exit_reason,
                    "hold_min": float(int(float(t.exit_time)) - int(float(t.entry_time))) / 60000,
                }
            )
    if not trades:
        print("无交易数据"); return
    tf = pd.DataFrame(trades)
    print(f"真实信号: {len(tf)} 笔, {tf.symbol.nunique()} 币")

    root = "data/market/candles"
    idx = pd.read_parquet(os.path.join(root, "archive_index.parquet"))
    con = duckdb.connect(":memory:")
    con.execute("SET TimeZone='UTC'")
    reader = SymbolReader(root, idx, con)

    out_rows = []
    for sym in tf["symbol"].unique():
        sym_t = tf[tf["symbol"] == sym]
        intervals = []
        for r in sym_t.itertuples():
            t0 = r.signal_time - BOX_LOOKBACK - PRE_BUFFER
            t1 = r.signal_time + AFTER_WINDOW
            intervals.append((t0, t1))
        reader.read_symbol(sym, intervals)
        for r in sym_t.itertuples():
            try:
                m = compute_signal_metrics(reader, sym, r.signal_time, r.trigger_price)
            except Exception as exc:  # noqa: BLE001
                print(f"  [{sym}] 指标失败: {exc}"); m = {}
            row = {
                "symbol": sym,
                "signal_time_utc": dt.datetime.utcfromtimestamp(r.signal_time / 1000),
                "trigger_price": r.trigger_price,
                "net_pnl": r.net_pnl,
                "exit_reason": r.exit_reason,
                "hold_min": r.hold_min,
            }
            row.update(m)
            out_rows.append(row)

    out_df = pd.DataFrame(out_rows)
    core = ["symbol", "signal_time_utc", "trigger_price", "net_pnl", "exit_reason", "hold_min"]
    metric_cols = [c for c in out_df.columns if c not in core]
    out_df = out_df[core + sorted(metric_cols)]
    out_df.to_csv("reports/spike_signal_shortterm_metrics.csv", index=False)
    print(f"完成: {len(out_df)} 行 -> reports/spike_signal_shortterm_metrics.csv")


if __name__ == "__main__":
    main()