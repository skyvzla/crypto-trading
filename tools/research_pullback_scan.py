"""研究：大插针回落接空 参数扫描（控制变量，一次读数据多次模拟）。

维度：
- 上涨幅度 min_spike_rise
- 回吐比例 retrace_frac
- 止盈 take_profit
- 持有上限 max_hold_seconds
- 等待回落 wait_seconds
- 起涨周期 min_rise_duration_hours / lookback_hours
- 退出开关 stop_5m_high / stop_15m_loss / 浮盈回撤
"""

from __future__ import annotations

import argparse
import itertools
import sys
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.research_pullback_short import (  # noqa: E402
    _detect_3s_spikes,
    _filter_rise_duration,
    _load_1m,
    _load_1s,
    _simulate_trades,
)


@dataclass(frozen=True)
class Combo:
    tag: str
    msr: float
    rf: float
    tp: float
    hold: int
    wait: int
    lookback_h: float
    dur_h: float
    s5h: bool = True
    s15m: bool = True
    dd_peak: float = 0.20
    dd_ratio: float = 0.10


BASE = dict(msr=0.30, rf=0.35, tp=0.10, hold=3600, wait=3600,
            lookback_h=24.0, dur_h=6.0, s5h=True, s15m=True,
            dd_peak=0.20, dd_ratio=0.10)


def _v(**over) -> Combo:
    kw = dict(BASE)
    kw.update(over)
    return Combo(**kw)


def build_combos() -> list[Combo]:
    combos: list[Combo] = []
    for msr in (0.25, 0.30, 0.40, 0.50):
        combos.append(_v(msr=msr, tag=f"msr={msr}"))
    for rf in (0.20, 0.25, 0.30, 0.35, 0.40, 0.50):
        combos.append(_v(rf=rf, tag=f"rf={rf}"))
    for tp in (0.05, 0.10, 0.15, 0.20):
        combos.append(_v(tp=tp, tag=f"tp={tp}"))
    for hold in (1800, 3600, 7200):
        combos.append(_v(hold=hold, tag=f"hold={hold}"))
    for wait in (1800, 3600, 7200):
        combos.append(_v(wait=wait, tag=f"wait={wait}"))
    for dur in (4.0, 6.0, 12.0):
        combos.append(_v(dur_h=dur, tag=f"dur={dur:g}h"))
    for lookback in (24.0, 48.0):
        combos.append(_v(lookback_h=lookback, tag=f"lookback={lookback:g}h"))
    combos.append(_v(s5h=False, tag="no_s5h"))
    combos.append(_v(s15m=False, tag="no_s15m"))
    combos.append(_v(s5h=False, s15m=False, tag="no_s5h_s15m"))
    combos.append(_v(dd_peak=0.15, dd_ratio=0.08, tag="dd=0.15/0.08"))
    combos.append(_v(dd_peak=0.30, dd_ratio=0.15, tag="dd=0.30/0.15"))
    combos.append(_v(dd_peak=0.0, tag="no_dd"))
    # ---- 组合轮：msr=0.40 为主干 ----
    for rf in (0.30, 0.35, 0.40, 0.50):
        combos.append(_v(msr=0.40, rf=rf, tag=f"m40_rf{rf}"))
    for tp in (0.15, 0.20):
        combos.append(_v(msr=0.40, tp=tp, tag=f"m40_tp{tp}"))
    combos.append(_v(msr=0.40, wait=7200, tag="m40_wait7200"))
    combos.append(_v(msr=0.40, dur_h=4.0, tag="m40_dur4h"))
    combos.append(_v(msr=0.40, tp=0.15, rf=0.40, tag="m40_tp15_rf40"))
    combos.append(_v(msr=0.40, tp=0.15, rf=0.50, tag="m40_tp15_rf50"))
    combos.append(_v(msr=0.40, tp=0.15, wait=7200, tag="m40_tp15_w7200"))
    combos.append(_v(msr=0.40, tp=0.15, dur_h=4.0, tag="m40_tp15_dur4"))
    combos.append(_v(msr=0.40, tp=0.20, rf=0.40, tag="m40_tp20_rf40"))
    combos.append(_v(msr=0.40, tp=0.20, rf=0.35, tag="m40_tp20_rf35"))
    combos.append(_v(msr=0.30, tp=0.15, tag="m30_tp15"))
    combos.append(_v(msr=0.30, tp=0.20, tag="m30_tp20"))
    combos.append(_v(msr=0.50, tp=0.15, tag="m50_tp15"))
    combos.append(_v(msr=0.40, tp=0.15, s5h=False, tag="m40_tp15_nos5h"))
    # ---- 细化轮 ----
    for msr in (0.35, 0.45):
        combos.append(_v(msr=msr, tag=f"msr={msr}"))
    for tp in (0.12, 0.18):
        combos.append(_v(msr=0.40, tp=tp, tag=f"m40_tp{tp}"))
    combos.append(_v(msr=0.40, dur_h=5.0, tag="m40_dur5h"))
    combos.append(_v(msr=0.40, tp=0.15, dur_h=5.0, tag="m40_tp15_dur5"))
    combos.append(_v(msr=0.40, tp=0.15, wait=5400, tag="m40_tp15_w5400"))
    combos.append(_v(msr=0.40, tp=0.15, rf=0.30, tag="m40_tp15_rf30"))
    combos.append(_v(msr=0.40, tp=0.20, wait=7200, tag="m40_tp20_w7200"))
    combos.append(_v(msr=0.40, tp=0.15, s15m=False, tag="m40_tp15_nos15m"))
    combos.append(_v(msr=0.40, tp=0.20, dur_h=5.0, tag="m40_tp20_dur5"))
    combos.append(_v(msr=0.35, tp=0.15, tag="m35_tp15"))
    combos.append(_v(msr=0.35, tp=0.20, tag="m35_tp20"))
    return combos


def process_symbol(job) -> list[dict]:
    index, symbol, start_ms, end_ms, combos = job
    df = _load_1s(index, symbol, start_ms, end_ms)
    if df.empty:
        return []
    event_idx = _detect_3s_spikes(
        df, rise_threshold=0.03, vol_multiple=2.0, cooldown_seconds=180
    )
    if len(event_idx) == 0:
        return []
    max_lookback_ms = int(max(c.lookback_h for c in combos) * 3_600_000)
    df_1m = _load_1m(index, symbol, start_ms - max_lookback_ms, end_ms)
    signal_arr = df["open_ms"].to_numpy(np.int64)
    out = []
    for combo in combos:
        ev = event_idx
        if combo.lookback_h > 0:
            keep = _filter_rise_duration(
                df_1m,
                signal_arr[ev],
                lookback_ms=int(combo.lookback_h * 3_600_000),
                min_duration_ms=int(combo.dur_h * 3_600_000),
            )
            ev = ev[keep]
        trades = _simulate_trades(
            df,
            ev.tolist(),
            pullback=0.0,
            take_profit=combo.tp,
            stop_loss=0,
            max_hold_seconds=combo.hold,
            wait_seconds=combo.wait,
            circuit="none",
            circuit_fill="high",
            min_spike_rise=combo.msr,
            retrace_frac=combo.rf,
            stop_5m_high=combo.s5h,
            stop_15m_loss=combo.s15m,
            drawdown_peak=combo.dd_peak,
            drawdown_ratio=combo.dd_ratio,
        )
        filled = [t for t in trades if t["entry_price"] is not None]
        n = len(filled)
        if n == 0:
            out.append({"tag": combo.tag, "n": 0})
            continue
        rets = np.array([t["return_pct"] for t in filled])
        wins = sum(1 for t in filled if t["reason"] in {"take_profit", "profit_drawdown"})
        out.append({
            "tag": combo.tag,
            "n": n,
            "wins": wins,
            "winrate": wins / n,
            "pnl_usdt": float(rets.sum() * 1000),
            "avg_ret_pct": float(rets.mean() * 100),
            "max_loss_pct": float(rets.min() * 100),
            "timeout_pct": sum(1 for t in filled if t["reason"] == "timeout") / n,
            "s5h_pct": sum(1 for t in filled if t["reason"] == "stop_5m_high") / n,
            "s15m_pct": sum(1 for t in filled if t["reason"] == "stop_15m_loss") / n,
        })
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--index", type=Path, default="data/market/candles/archive_index.parquet")
    parser.add_argument("--symbols-file", type=Path, default="/tmp/coins_big1.txt")
    parser.add_argument("--start", default="2025-08-01")
    parser.add_argument("--end", default="2026-08-01")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--output", type=Path, default="reports/research_pullback_scan.csv")
    args = parser.parse_args()

    symbols = [s.strip() for s in args.symbols_file.read_text().split() if s.strip()]
    combos = build_combos()
    start_ms = int(pd.Timestamp(args.start).timestamp() * 1000)
    end_ms = int(pd.Timestamp(args.end).timestamp() * 1000)

    jobs = [
        (args.index, symbol, start_ms, end_ms, combos)
        for symbol in symbols
    ]
    rows: list[dict] = []
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        for per_symbol in ex.map(process_symbol, jobs, chunksize=1):
            rows.extend(per_symbol)

    df = pd.DataFrame(rows)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(args.output, index=False)

    grouped = df.groupby("tag").agg(
        n=("n", "sum"),
        wins=("wins", "sum"),
        pnl=("pnl_usdt", "sum"),
        avg_ret=("avg_ret_pct", lambda s: float(np.mean([v for v in s if v == v]))),
        max_loss=("max_loss_pct", "min"),
    ).reset_index()
    grouped["wr"] = grouped["wins"] / grouped["n"].where(grouped["n"] > 0, np.nan)
    grouped = grouped.sort_values("pnl", ascending=False)
    print("\n=== 参数扫描汇总（按总 PnL 排序；wr=按笔加权胜率）===")
    print(grouped[["tag", "n", "wr", "pnl", "avg_ret", "max_loss"]].to_string(index=False))


if __name__ == "__main__":
    main()
