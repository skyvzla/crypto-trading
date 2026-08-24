"""W3-B: 平静期突发首根 spike 的做空价值研究。

特征（spike 确认时因果可得）:
  - first_spike_60: 前 60 根无其他 spike（突发首根）
  - pre_bbw_mult:   spike 前一根 bb_width / rolling240 中位（低 = 长期平静）
  - vol_mult:       spike bar 成交量 / 前 60 根中位
spike 定义两档: >=15% 严格 / >=10% 宽松。
做空路径模拟: 下一根 open 入场, SL{2,3,5}% x HOLD{15,30,60}m 先触优先, 费 0.1%。

用法:
    .venv/bin/python tools/run_pre_spike_stage3.py [--workers 10]
"""

from __future__ import annotations

import argparse
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

ROOT = Path("data/market/candles")
TRAIN_END = pd.Timestamp("2026-04-30 23:00:00+00:00")
TEST_START = pd.Timestamp("2026-05-01 00:00:00+00:00")
SPIKE_LEVELS = (0.15, 0.10)
FIRST_SPIKE_GAP = 60
FEE_SLIP = 0.001
SL_GRID = (0.02, 0.03, 0.05)
HOLD_MINUTES = (15, 30, 60)


def _simulate_short(bars: pd.DataFrame, entry_idx: int) -> dict[str, float] | None:
    times = bars["open_ms"].to_numpy(np.int64)
    if entry_idx + 1 >= len(bars):
        return None
    entry_ms = int(times[entry_idx + 1])
    entry = float(bars.iloc[entry_idx + 1]["open"])
    end_idx = np.searchsorted(times, entry_ms + max(HOLD_MINUTES) * 60_000, side="right")
    horizon_end = min(end_idx, len(bars))
    if horizon_end - (entry_idx + 1) < 3:
        return None
    highs = bars["high"].to_numpy(float)[entry_idx + 1 : horizon_end]
    closes = bars["close"].to_numpy(float)[entry_idx + 1 : horizon_end]
    stamp_ms = times[entry_idx + 1 : horizon_end]
    out: dict[str, float] = {}
    for sl in SL_GRID:
        for minutes in HOLD_MINUTES:
            cutoff = entry_ms + minutes * 60_000
            visible = stamp_ms < cutoff
            n_vis = int(visible.sum())
            if n_vis < 3:
                out[f"sl{int(sl*100)}_h{minutes}"] = np.nan
                continue
            h_seg = highs[:n_vis] / entry
            hit = h_seg >= 1.0 + sl
            if hit.any():
                out[f"sl{int(sl*100)}_h{minutes}"] = -sl - FEE_SLIP
            else:
                last = closes[n_vis - 1] / entry
                out[f"sl{int(sl*100)}_h{minutes}"] = (1.0 - last) - FEE_SLIP
    mfe_window = highs / entry
    out["mfe30"] = float(max(0.0, 1.0 - mfe_window.min())) if len(mfe_window) else np.nan
    out["mae30"] = float(mfe_window.max() - 1.0) if len(mfe_window) else np.nan
    return out


def _process_symbol(symbol: str) -> pd.DataFrame | None:
    con = duckdb.connect()
    try:
        bars = con.execute(
            """SELECT epoch_ms(open_time)::BIGINT AS open_ms, open, high, low, close, volume
               FROM read_parquet(?, union_by_name=true) ORDER BY open_time""",
            [str(ROOT / symbol / "1m" / "**" / "*.parquet")],
        ).fetchdf()
    except Exception:
        return None
    finally:
        con.close()
    if len(bars) < 500:
        return None

    o = bars["open"].to_numpy(float)
    h = bars["high"].to_numpy(float)
    l = bars["low"].to_numpy(float)
    c = bars["close"].to_numpy(float)
    v = pd.to_numeric(bars["volume"], errors="coerce").to_numpy(float)
    tp = (h + l + c) / 3.0
    sma20 = pd.Series(tp).rolling(20, min_periods=20).mean()
    std20 = pd.Series(tp).rolling(20, min_periods=20).std(ddof=1)
    bbw = (4 * std20 / sma20).to_numpy()
    bbw_base = pd.Series(bbw).rolling(240, min_periods=240).median().shift(1).to_numpy()
    vol_base = pd.Series(v).rolling(FIRST_SPIKE_GAP, min_periods=FIRST_SPIKE_GAP).median().shift(1).to_numpy()

    prev_c = np.roll(c, 1)
    prev_c[0] = c[0]
    timestamps = bars["open_ms"].to_numpy(np.int64)
    cutoff_ms = int(TRAIN_END.timestamp() * 1_000)
    test_start_ms = int(TEST_START.timestamp() * 1_000)

    rows: list[dict[str, object]] = []
    for level in SPIKE_LEVELS:
        spike_positions = np.where(h >= prev_c * (1.0 + level))[0]
        for idx in spike_positions:
            if idx < 250 or idx + 2 > len(bars):
                continue
            segment = "train" if timestamps[idx] < cutoff_ms else ("test" if timestamps[idx] >= test_start_ms else "embargo")
            if segment == "embargo":
                continue
            pre_bbw_mult = bbw[idx - 1] / bbw_base[idx - 1] if np.isfinite(bbw_base[idx - 1]) and bbw_base[idx - 1] > 0 else np.nan
            vol_mult = v[idx] / vol_base[idx] if np.isfinite(vol_base[idx]) and vol_base[idx] > 0 else np.nan
            prior_spikes = spike_positions[(spike_positions < idx) & (spike_positions >= idx - FIRST_SPIKE_GAP)]
            row: dict[str, object] = {
                "symbol": symbol,
                "level": level,
                "segment": segment,
                "open_ms": int(timestamps[idx]),
                "first_spike_60": len(prior_spikes) == 0,
                "pre_bbw_mult": float(pre_bbw_mult),
                "vol_mult": float(vol_mult),
                "spike_gain": float(h[idx] / c[idx - 1] - 1.0),
            }
            outcome = _simulate_short(bars, idx)
            if outcome is not None:
                row.update(outcome)
            rows.append(row)
    return pd.DataFrame(rows) if rows else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=10)
    args = parser.parse_args(argv)
    started = time.monotonic()

    symbols = sorted({p.parents[4].name for p in ROOT.glob("*/1m/2026/**/*.parquet")})
    print(f"symbols: {len(symbols)}", flush=True)
    parts: list[pd.DataFrame] = []
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        for frame in pool.map(_process_symbol, symbols, chunksize=8):
            if frame is not None and not frame.empty:
                parts.append(frame)
    events = pd.concat(parts, ignore_index=True)
    out_path = Path("/tmp/opencode/w3b_events.parquet")
    events.to_parquet(out_path, index=False)
    print(
        f"事件数: level15={int((events['level'] == 0.15).sum())} "
        f"level10={int((events['level'] == 0.10).sum())} 耗时 {time.monotonic()-started:.0f}s -> {out_path}",
        flush=True,
    )

    summary = summarize(events)
    report = Path("docs/research/PRE_SPIKE_STUDY_W3B.md")
    report.write_text(summary, encoding="utf-8")
    print(f"报告: {report}")
    return 0


def _cell(sub: pd.DataFrame, columns: list[str]) -> dict[str, object]:
    row: dict[str, object] = {"n": len(sub)}
    if sub.empty:
        return row
    sl3_h30 = sub.get("sl3_h30")
    if sl3_h30 is not None and sl3_h30.notna().any():
        values = sl3_h30.dropna()
        row["sl3h30_avg"] = round(float(values.mean()), 4)
        row["sl3h30_win"] = round(float((values > 0).mean()), 3)
    mfe30 = sub.get("mfe30")
    if mfe30 is not None and mfe30.notna().any():
        row["mfe30_med"] = round(float(mfe30.median()), 4)
    for col in columns:
        if col in sub.columns and sub[col].notna().any():
            row[f"{col}_med"] = round(float(sub[col].median()), 3)
    return row


def summarize(events: pd.DataFrame) -> str:
    lines = [
        "# W3-B: 平静期突发首根 spike 的做空价值", "",
        f"- train < {TRAIN_END.date()} / test >= {TEST_START.date()}",
        "- spike 定义: high ≥ prev_close × (1+level)；level ∈ {15%, 10%}",
        "- 特征全部在 spike 确认时因果可得；模拟为做空下一根 open，SL×HOLD 先触优先，费 0.1%",
        "",
    ]
    for level in SPIKE_LEVELS:
        lines += [f"## spike ≥ {level:.0%}", ""]
        base = events[(events["level"] == level)]
        for segment in ("train", "test"):
            seg = base[base["segment"] == segment]
            if seg.empty:
                continue
            all_row = _cell(seg, ["pre_bbw_mult", "vol_mult"])
            first = seg[seg["first_spike_60"]]
            calm_first = first[first["pre_bbw_mult"] < 0.8]
            calm_first_vol = calm_first[calm_first["vol_mult"] > 5.0]
            lines += [
                f"### {segment} 段", "",
                "| 分层 | n | sl3_h30 avg | win | mfe30 中位 |",
                "|---|---:|---:|---:|---:|",
                f"| 全部 spike | {all_row['n']} | {all_row.get('sl3h30_avg', '-')} | {all_row.get('sl3h30_win', '-')} | {all_row.get('mfe30_med', '-')} |",
                f"| 首根(前60根无spike) | {_cell(first, [])['n']} | {_cell(first, []).get('sl3h30_avg', '-')} | {_cell(first, []).get('sl3h30_win', '-')} | {_cell(first, []).get('mfe30_med', '-')} |",
                f"| 首根 & pre_bbw<0.8x | {_cell(calm_first, [])['n']} | {_cell(calm_first, []).get('sl3h30_avg', '-')} | {_cell(calm_first, []).get('sl3h30_win', '-')} | {_cell(calm_first, []).get('mfe30_med', '-')} |",
                f"| 首根 & bbw<0.8x & vol>5x | {_cell(calm_first_vol, [])['n']} | {_cell(calm_first_vol, []).get('sl3h30_avg', '-')} | {_cell(calm_first_vol, []).get('sl3h30_win', '-')} | {_cell(calm_first_vol, []).get('mfe30_med', '-')} |",
                "",
            ]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
