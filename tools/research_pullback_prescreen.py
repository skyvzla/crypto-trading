#!/usr/bin/env python3
"""预筛：统计候选币在窗口内的 3s 暴涨 + 大插针(总涨幅>=门槛)事件数。

只做事件检测（不模拟成交），走 archive sidecar index 部分读取，多进程并行。
输出 CSV：symbol, 3s 暴涨事件数, 其中插针总涨幅>=门槛的事件数。
"""

from __future__ import annotations

import argparse
import concurrent.futures
import functools
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

import sys

TOOLS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS_DIR))
from research_pullback_short import (  # noqa: E402
    _detect_3s_spikes,
    _load_1s,
)


def _count_big(symbol: str, args: argparse.Namespace) -> dict:
    df = _load_1s(args.index, symbol, args.start_ms, args.end_ms)
    if df.empty:
        return {"symbol": symbol, "events": 0, "big_events": 0}
    idx = _detect_3s_spikes(
        df,
        rise_threshold=args.rise_threshold,
        vol_multiple=args.vol_multiple,
        cooldown_seconds=args.cooldown_seconds,
    )
    if idx.size == 0:
        return {"symbol": symbol, "events": 0, "big_events": 0}
    open_ms = df["open_ms"].to_numpy(np.int64)
    high = df["high"].to_numpy(float)
    low = df["low"].to_numpy(float)
    close = df["close"].to_numpy(float)
    n = len(df)
    big = 0
    for s in idx:
        origin = close[s - 3]
        if not np.isfinite(origin) or origin <= 0:
            continue
        sh = 0.0
        for j in range(s, n):
            if open_ms[j] - open_ms[s] > args.wait_ms:
                break
            sh = max(sh, high[j])
        if sh / origin - 1.0 >= args.min_spike_rise:
            big += 1
    return {"symbol": symbol, "events": int(len(idx)), "big_events": big}


def main() -> None:
    parser = argparse.ArgumentParser(description="大插针币池预筛")
    parser.add_argument("--index", type=Path, default="data/market/candles/archive_index.parquet")
    parser.add_argument("--symbols", nargs="+", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--output", type=Path, default="reports/research_pullback_prescreen.csv")
    parser.add_argument("--rise-threshold", type=float, default=0.03)
    parser.add_argument("--vol-multiple", type=float, default=2.0)
    parser.add_argument("--cooldown-seconds", type=int, default=180)
    parser.add_argument("--min-spike-rise", type=float, default=0.30)
    parser.add_argument("--wait-seconds", type=int, default=300)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()
    args.start_ms = int(pd.Timestamp(args.start).timestamp() * 1000)
    args.end_ms = int(pd.Timestamp(args.end).timestamp() * 1000)
    args.wait_ms = args.wait_seconds * 1000

    rows = []
    worker = functools.partial(_count_big, args=args)
    with concurrent.futures.ProcessPoolExecutor(max_workers=args.workers) as pool:
        for row in pool.map(worker, args.symbols, chunksize=1):
            rows.append(row)
    df = pd.DataFrame(rows).sort_values("big_events", ascending=False)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(args.output, index=False)
    print(df.head(40).to_string(index=False))
    print(f"共 {len(df)} 币, 输出: {args.output}")


if __name__ == "__main__":
    main()
