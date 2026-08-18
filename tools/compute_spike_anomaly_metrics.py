"""异动事件完整指标矩阵计算。

输入: reports/daily_amplitude_over_50pct.csv (异动事件清单)
输出: reports/spike_anomaly_metrics.csv (事件 × 指标矩阵)

读取方式: 按 symbol 分块，一次读入该 symbol 所有事件需要的时间跨度（合并连续区间），
再对每个事件按振荡区间重心计算指标。避免全量扫描。
"""
from __future__ import annotations

import argparse
import datetime as dt
import math
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
    sma,
    stochastic,
    vwap_dev,
    winsorized_mean,
)

MS_MIN = 60_000
MS_5M = 5 * MS_MIN
MS_15M = 15 * MS_MIN
MS_1H = 60 * MS_MIN
MS_DAY = 24 * MS_1H

BOX_LOOKBACK = 3 * MS_DAY      # 箱体前视窗口
AFTER_WINDOW = 24 * MS_1H      # 异动结束后回看窗口（评估后续走势）
PRE_BUFFER = 1 * MS_DAY        # 区间前额外缓冲（趋势/前高）
TF = {"5m": MS_5M, "15m": MS_15M, "1h": MS_1H}


def parse_ms(iso: str) -> int:
    return int(pd.Timestamp(iso).timestamp() * 1000)


class SymbolReader:
    """按 symbol 分块：一次读入该 symbol 所有需要时间跨度的 1m 数据。"""

    def __init__(self, root: str, index: pd.DataFrame, con: duckdb.DuckDBPyConnection):
        self.root = root
        self.con = con
        self.idx1m = index[index["timeframe"] == "1m"]
        self.cache: dict[str, tuple[int, int, dict[int, tuple]]] = {}

    def read_symbol(self, symbol: str, intervals: list[tuple[int, int]]) -> None:
        """读入该 symbol 覆盖所有 intervals 的 1m 数据（合并区间，分块）。"""
        # 合并重叠区间
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


def ohlcv_rows(rows: list[tuple]) -> tuple[np.ndarray, ...]:
    if not rows:
        return np.array([]), np.array([]), np.array([]), np.array([]), np.array([]), np.array([])
    a = np.array(rows)
    return a[:, 0].astype(np.int64), a[:, 1], a[:, 2], a[:, 3], a[:, 4], a[:, 5]  # t, o, h, l, c, v


def compute_box(highs: np.ndarray, lows: np.ndarray) -> dict:
    """箱体/通道多方法指标。highs/lows 为 1h 聚合数组。"""
    if len(highs) < 8:
        return {}
    slope_bps, r2 = linear_slope_r2(highs)
    out = {
        "slope_bps": round(slope_bps, 2),
        "r2": round(r2, 3),
    }
    for name, p in (("p90", 0.90), ("p95", 0.95), ("p99", 0.99)):
        out[f"up_{name}"] = round(percentile(highs, p), 8)
    out["up_win"] = round(winsorized_mean(highs), 8)
    # 回归 ±1.5σ
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


def compute_event_metrics(
    reader: SymbolReader, symbol: str, ev: dict, days_px: dict[dt.date, list]
) -> dict:
    """计算单个异动事件的完整指标。"""
    res: dict = {}
    osc_start = parse_ms(ev["oscillation_start_utc"])
    osc_end = parse_ms(ev["oscillation_end_utc"])
    day_start = parse_ms(ev["open_time_utc"])

    # ---- 1. 箱体/通道（振荡区间起点前 3d，1h 粒度） ----
    box_t0 = osc_start - BOX_LOOKBACK
    rows = reader.slice(symbol, box_t0, osc_start)
    if len(rows) >= 24 * 6:  # 至少 6h 的 1m
        t, o, h, l, c, v = ohlcv_rows(rows)
        ho, hh, hl, hc, hv, ht = aggregate_ohlcv(o, h, l, c, v, t, MS_1H)
        box = compute_box(hh, hl)
        res.update({f"box_{k}": v for k, v in box.items()})
        # 区间起价相对箱体
        if "box_up_p90" in res:
            res["osc_start_over_up"] = round(float(ev["oscillation_start_price"]) / res["box_up_p90"], 3)
            res["osc_end_over_up"] = round(float(ev["oscillation_end_price"]) / res["box_up_p90"], 3)

    # ---- 2. 振荡区间内部：动量/量能/波动（5m 主 / 15m 辅） ----
    rows = reader.slice(symbol, osc_start, osc_end)
    if len(rows) >= 60:
        t, o, h, l, c, v = ohlcv_rows(rows)
        # 上轨触及统计：振荡区间内逐 1m bar high 打到箱体上沿的次数
        # （突破越多，三高/异动指标作为稀缺信号的有效性越差）
        if h.size:
            for name in ("up_p90", "up_p95", "up_p99", "up_win", "up_reg15"):
                key = f"box_{name}"
                if key not in res or not res[key] == res[key]:
                    continue
                up = float(res[key])
                hit = h >= up
                hits = int(np.sum(hit))
                res[f"touch_{name}_count"] = hits
                res[f"touch_{name}_share"] = round(hits / h.size, 4)
                first = int(np.argmax(hit)) if hits else -1
                res[f"touch_{name}_first_min"] = (
                    round(float(t[first] - osc_start) / MS_MIN, 1) if first >= 0 else None
                )
            # 三高触发价（= 振荡区间最后一根 1m bar 的 close，指标按最后一个 bar 计算）
            # 在行情周期内被触及的次数：触及越多，触发价越普通、信号越不稀缺。
            # 窗口含收尾 1 根（触发价所在 bar 的 open_time == osc_end，半开区间会排除）。
            tail_rows = reader.slice(symbol, osc_start, osc_end + MS_MIN)
            if len(tail_rows) >= 2:
                tt, to, th, tl, tc, tv = ohlcv_rows(tail_rows)
                trig = float(tc[-1])
                if trig > 0:
                    hit_trig = th >= trig
                    hits_trig = int(np.sum(hit_trig))
                    res["touch_trigger_count"] = hits_trig
                    res["touch_trigger_share"] = round(hits_trig / th.size, 4)
                    res["touch_trigger_close_count"] = int(np.sum(tc >= trig))
                    first_trig = int(np.argmax(hit_trig)) if hits_trig else -1
                    res["touch_trigger_first_min"] = (
                        round(float(tt[first_trig] - osc_start) / MS_MIN, 1)
                        if first_trig >= 0
                        else None
                    )
        # 5m
        o5, h5, l5, c5, v5, t5 = aggregate_ohlcv(o, h, l, c, v, t, MS_5M)
        # 15m
        o15, h15, l15, c15, v15, t15 = aggregate_ohlcv(o, h, l, c, v, t, MS_15M)

        if len(c5) >= 16:
            res["rsi_5m"] = round(float(rsi(c5, 14)[-1]), 1)
            _, _, mh = macd(c5, 12, 26, 9)
            res["macd_hist_5m"] = round(float(mh[-1]), 8)
            res["roc_5m"] = round(float(roc(c5, 5)[-1]), 3)
            res["cci_5m"] = round(cci(h5, l5, c5, 20), 1)
            k, _ = stochastic(c5, 14)
            res["sto_k_5m"] = round(k, 1)
            ema20 = ema(c5, 20)[-1]
            res["ema_ratio_5m"] = round(float(c5[-1] / ema20), 4) if ema20 > 0 else float("nan")
            res["bb_width_5m"] = round(float(bb_width(c5, 20, 2.0)[3][-1]), 4)
            a5 = atr(h5, l5, c5, 14)[-1]
            res["atr_ratio_5m"] = round(float(a5 / c5[-1]), 5) if c5[-1] > 0 else float("nan")
            res["obv_slope_5m"] = round(obv_slope(c5, v5, 20), 2)
            res["vwap_dev_5m"] = round(vwap_dev(o5, h5, l5, c5, v5, 20), 2)

        if len(c15) >= 16:
            res["rsi_15m"] = round(float(rsi(c15, 14)[-1]), 1)
            res["roc_15m"] = round(float(roc(c15, 5)[-1]), 3)
            _, _, mh15 = macd(c15, 12, 26, 9)
            res["macd_hist_15m"] = round(float(mh15[-1]), 8)

        # 量能
        if len(v) >= 60:
            res["vol_cv_1h"] = round(float(np.std(v[-60:]) / np.mean(v[-60:])), 3)
            up = sum(1 for i in range(1, len(c)) if c[i] > c[i - 1])
            res["green_share_1m"] = round(up / (len(c) - 1), 3)
        # 拉升形态（1m）
        if len(c) >= 6:
            ret5 = c[-5] and (c[-1] / c[-5] - 1) or 0
            ret_prev5 = (c[-6] / c[-10] - 1) if len(c) >= 10 else 0
            res["accel_5m"] = round(float(ret5 - ret_prev5), 4)
            res["pulse_1m"] = round(float(np.max(np.diff(c[-30:]) / c[-30:-1]) if len(c) >= 31 else np.max(np.diff(c) / c[:-1])), 5)
        # 连续阳线
        n = 0
        for i in range(len(c) - 1, 0, -1):
            if c[i] > c[i - 1]:
                n += 1
            else:
                break
        res["consecutive_green"] = n

    # ---- 3. 异动后走势（振荡结束后 15m/30m/45m/1h/4h/24h 的累计涨跌） ----
    rows = reader.slice(symbol, osc_end, osc_end + AFTER_WINDOW)
    if len(rows) >= 5:
        times = [r[0] for r in rows]
        closes = [r[4] for r in rows]
        highs = [r[2] for r in rows]
        base = closes[0]
        for label, mins in (("15m", 15), ("30m", 30), ("45m", 45), ("1h", 60), ("4h", 240), ("24h", 1440)):
            target = osc_end + mins * MS_MIN
            idx = next((i for i, tt in enumerate(times) if tt >= target), None)
            ref = closes[idx] if idx is not None else closes[-1]
            res[f"ret_after_{label}"] = round(float(ref / base - 1.0) * 100.0, 2)
        # 短周期最大不利（空头视角：异动后短窗口最高价）
        for label, mins in (("15m", 15), ("30m", 30)):
            target = osc_end + mins * MS_MIN
            idx = next((i for i, tt in enumerate(times) if tt >= target), None)
            window_highs = highs[: idx + 1] if idx is not None else highs
            res[f"fwd_max_{label}"] = round(float(max(window_highs) / base - 1.0) * 100.0, 2)
        res["max_high_after"] = round(float(max(r[2] for r in rows)), 8)
        res["min_low_after"] = round(float(min(r[3] for r in rows)), 8)
        res["ret_after_max"] = round(float(max(r[2] for r in rows) / base - 1.0) * 100.0, 2)
        res["ret_after_min"] = round(float(min(r[3] for r in rows) / base - 1.0) * 100.0, 2)

    return res


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", default="reports/daily_amplitude_over_50pct.csv")
    ap.add_argument("--out", default="reports/spike_anomaly_metrics.csv")
    ap.add_argument("--limit-symbols", type=int, default=0, help="仅处理前 N 个 symbol（调试用）")
    args = ap.parse_args()

    root = "data/market/candles"
    idx = pd.read_parquet(os.path.join(root, "archive_index.parquet"))
    con = duckdb.connect(":memory:")
    con.execute("SET TimeZone='UTC'")
    reader = SymbolReader(root, idx, con)

    events = pd.read_csv(args.events)
    print(f"事件总数: {len(events)}")

    # 按 symbol 分组处理
    symbols = events["symbol"].unique()
    if args.limit_symbols:
        symbols = symbols[: args.limit_symbols]
    print(f"symbol 数: {len(symbols)}")

    out_rows = []
    for si, sym in enumerate(symbols):
        sym_ev = events[events["symbol"] == sym]
        # 跳过 oscillation 时间缺失的行
        sym_ev = sym_ev[sym_ev["oscillation_start_utc"].notna() & sym_ev["oscillation_end_utc"].notna()]
        if sym_ev.empty:
            continue
        # 收集该 symbol 全部事件需要的区间（振荡 ± 缓冲），一次读入
        intervals = []
        for _, ev in sym_ev.iterrows():
            osc_start = parse_ms(ev["oscillation_start_utc"])
            osc_end = parse_ms(ev["oscillation_end_utc"])
            intervals.append((osc_start - BOX_LOOKBACK - PRE_BUFFER, osc_end + AFTER_WINDOW))
        reader.read_symbol(sym, intervals)

        for _, ev in sym_ev.iterrows():
            try:
                m = compute_event_metrics(reader, sym, ev.to_dict(), None)
            except Exception as exc:  # noqa: BLE001
                print(f"  [{sym} {ev['open_time_utc'][:10]}] 指标计算失败: {exc}")
                m = {}
            row = {
                "symbol": sym,
                "event_day": ev["open_time_utc"][:10],
                "osc_start_utc": ev["oscillation_start_utc"],
                "osc_end_utc": ev["oscillation_end_utc"],
                "direction": ev["oscillation_direction"],
                "amplitude_pct": ev["amplitude_percent"],
                "osc_duration_min": ev["oscillation_duration_minutes"],
            }
            row.update(m)
            out_rows.append(row)
        if (si + 1) % 50 == 0:
            print(f"  已处理 {si + 1}/{len(symbols)} symbol")

    out_df = pd.DataFrame(out_rows)
    # 统一列顺序：核心在前，指标按字母
    core = ["symbol", "event_day", "osc_start_utc", "osc_end_utc", "direction", "amplitude_pct", "osc_duration_min"]
    metric_cols = [c for c in out_df.columns if c not in core]
    out_df = out_df[core + sorted(metric_cols)]
    out_df.to_csv(args.out, index=False)
    print(f"完成: {len(out_df)} 行 -> {args.out}")


if __name__ == "__main__":
    main()