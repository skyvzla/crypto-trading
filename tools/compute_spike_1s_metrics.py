"""异动事件 1s 周期指标补充。

策略主判断周期是 1s（rise_5s ≥ 5% + 5s 量 ≥ 中位量×5），因此对每个异动事件
补充 1s 粒度特征，与策略口径一致：

- osc_end 前 1s 特征（异动收尾时刻）：
    rise_5s / rise_10s / rise_30s / rise_60s  : 对应窗口累计涨幅%
    vol_mult_5s / vol_mult_30s               : 窗口量 / (前 5 分钟中位秒量 × 窗口秒数)
    pulse_1s_max                             : 最后 5 分钟单秒最大涨幅%
    spike_seconds_ratio                      : 最后 30 分钟有成交秒占比（流动性）
- osc_end 后 1s 走势（做空视角，最大不利）：
    fwd_max_1s_15m / fwd_max_1s_30m          : 后 15m/30m 内 1s 最高价相对 osc_end close 涨幅%
    fwd_max_1s_60s                           : 后 60s 内最高价涨幅%

输出: reports/spike_anomaly_metrics_1s.csv
读取: 按 symbol 分块（1s 按天分区，读 osc_end ± 窗口所需文件），不全量。
"""
from __future__ import annotations

import os
import sys

import duckdb
import numpy as np
import pandas as pd

MS_MIN = 60_000
MS_SEC = 1_000

PRE_1S = 30 * MS_MIN        # osc_end 前 30 分钟（秒级流动性 + 脉冲）
AFTER_1S = 30 * MS_MIN       # osc_end 后 30 分钟（最大不利）
DAY_MS = 24 * 60 * MS_MIN


def parse_ms(iso: str) -> int:
    return int(pd.Timestamp(iso).timestamp() * 1000)


class SymbolReader1s:
    """按 symbol 分块读入 1s 数据（有成交秒 Bar，trade-active）。"""

    def __init__(self, root: str, index: pd.DataFrame, con: duckdb.DuckDBPyConnection):
        self.root = root
        self.con = con
        self.idx1s = index[index["timeframe"] == "1s"]
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
                for v in self.idx1s[
                    (self.idx1s["symbol"] == symbol)
                    & (self.idx1s["first_open_ms"] < t1)
                    & (self.idx1s["last_close_ms"] >= t0)
                ]["relative_path"]
            ]
            if not files:
                continue
            rows = self.con.execute(
                """SELECT epoch_ms(open_time),open,high,low,close,volume
                   FROM read_parquet(?, union_by_name=true)
                   WHERE symbol=? AND timeframe='1s'
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


def compute_1s_metrics(reader: SymbolReader1s, symbol: str, osc_end: int) -> dict:
    res: dict = {}
    # ---- 1. osc_end 前 30 分钟 1s 特征 ----
    rows = reader.slice(symbol, osc_end - PRE_1S, osc_end)
    if len(rows) >= 60:
        times = [r[0] for r in rows]
        closes = [float(r[4]) for r in rows]
        volumes = [float(r[5]) for r in rows]
        highs = [float(r[2]) for r in rows]
        last = closes[-1]
        if last > 0:
            for label, secs in (("5s", 5), ("10s", 10), ("30s", 30), ("60s", 60)):
                tgt = times[-1] - secs * MS_SEC
                idx = next((i for i, tt in enumerate(times) if tt >= tgt), 0)
                ref = closes[idx] if idx < len(closes) else closes[0]
                res[f"rise_{label}"] = round((last / ref - 1.0) * 100.0, 3)
        # 成交量倍数：窗口量 / (前 5 分钟中位秒量 × 窗口秒数)
        base_vols = sorted(volumes[: -300]) if len(volumes) > 300 else sorted(volumes)
        if base_vols:
            median_vol = base_vols[len(base_vols) // 2]
            if median_vol > 0:
                for label, secs in (("5s", 5), ("30s", 30)):
                    tgt = times[-1] - secs * MS_SEC
                    idx = next((i for i, tt in enumerate(times) if tt >= tgt), 0)
                    win_vol = sum(volumes[idx:])
                    res[f"vol_mult_{label}"] = round(win_vol / (median_vol * secs), 2)
        # 单秒最大脉冲（最后 5 分钟）
        tail = closes[-300:] if len(closes) >= 300 else closes
        diffs = [tail[i] / tail[i - 1] - 1.0 for i in range(1, len(tail)) if tail[i - 1] > 0]
        res["pulse_1s_max"] = round(max(diffs) * 100.0, 3) if diffs else 0.0
        # 有成交秒占比（最后 30 分钟）
        res["spike_seconds_ratio"] = round(len(rows) / 1800.0, 3)

    # ---- 2. osc_end 后 1s 走势（做空视角最大不利） ----
    rows = reader.slice(symbol, osc_end, osc_end + AFTER_1S)
    if len(rows) >= 10:
        times = [r[0] for r in rows]
        highs = [float(r[2]) for r in rows]
        base = highs[0]
        if base > 0:
            for label, mins in (("60s", 1), ("15m", 15), ("30m", 30)):
                tgt = osc_end + mins * MS_MIN
                idx = next((i for i, tt in enumerate(times) if tt >= tgt), len(times) - 1)
                win_highs = highs[: idx + 1]
                res[f"fwd_max_1s_{label}"] = round((max(win_highs) / base - 1.0) * 100.0, 3)
    return res


def main() -> None:
    events = pd.read_csv("reports/daily_amplitude_over_50pct.csv")
    events = events[events["oscillation_start_utc"].notna() & events["oscillation_end_utc"].notna()]
    print(f"事件总数: {len(events)}")

    root = "data/market/candles"
    idx = pd.read_parquet(os.path.join(root, "archive_index.parquet"))
    con = duckdb.connect(":memory:")
    con.execute("SET TimeZone='UTC'")
    reader = SymbolReader1s(root, idx, con)

    out_rows = []
    symbols = sorted(events["symbol"].unique())
    for si, sym in enumerate(symbols):
        sym_ev = events[events["symbol"] == sym]
        intervals = []
        for r in sym_ev.itertuples():
            osc_end = parse_ms(r.oscillation_end_utc)
            intervals.append((osc_end - PRE_1S, osc_end + AFTER_1S))
        reader.read_symbol(sym, intervals)
        for r in sym_ev.itertuples():
            osc_end = parse_ms(r.oscillation_end_utc)
            m = compute_1s_metrics(reader, sym, osc_end)
            row = {"symbol": sym, "event_day": r.open_time_utc[:10], "direction": r.oscillation_direction,
                   "osc_end_utc": r.oscillation_end_utc, "amplitude_pct": r.amplitude_percent}
            row.update(m)
            out_rows.append(row)
        if (si + 1) % 50 == 0:
            print(f"  已处理 {si + 1}/{len(symbols)} symbol")

    out_df = pd.DataFrame(out_rows)
    core = ["symbol", "event_day", "direction", "osc_end_utc", "amplitude_pct"]
    metric_cols = [c for c in out_df.columns if c not in core]
    out_df = out_df[core + sorted(metric_cols)]
    out_df.to_csv("reports/spike_anomaly_metrics_1s.csv", index=False)
    print(f"完成: {len(out_df)} 行 -> reports/spike_anomaly_metrics_1s.csv")


if __name__ == "__main__":
    main()