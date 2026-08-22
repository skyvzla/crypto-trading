"""W3: pre-spike 前兆预警信号完整研究。

全市场全历史 1m K线，atr_mult × wick 阈值网格的条件概率研究，
train/test 时间外推 + spike 确认后的路径级成本模拟。

用法:
    .venv/bin/python tools/run_pre_spike_study.py [--workers 10] [--out docs/research/PRE_SPIKE_STUDY_W3.md]
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
ATR_MULT_GRID = (2.0, 3.0, 5.0)
WICK_GRID = (1.0, 2.0, 4.0)
BBW_THRESHOLD = 3.0
HORIZONS = (5, 10, 20)
SPIKE_RATIO = 1.15
FEE_SLIP = 0.001
SL_GRID = (0.02, 0.03, 0.05)
HOLD_MINUTES = (15, 30, 60)


def _future_spike_mask(spike: np.ndarray, horizon: int) -> np.ndarray:
    """未来 horizon 根（i+1..i+horizon）内是否出现 spike；不含当前 bar。"""
    series = pd.Series(spike.astype(float))
    forward = (
        series.iloc[::-1]
        .rolling(horizon + 1, min_periods=horizon + 1)
        .sum()
        .iloc[::-1]
        .to_numpy()
    )
    return np.nan_to_num(forward - spike.astype(float), nan=0.0) > 0


def _process_symbol(symbol: str) -> dict[str, object] | None:
    con = duckdb.connect()
    try:
        bars = con.execute(
            """SELECT epoch_ms(open_time)::BIGINT AS open_ms, open, high, low, close
               FROM read_parquet(?, union_by_name=true) ORDER BY open_time""",
            [str(ROOT / symbol / "1m" / "**" / "*.parquet")],
        ).fetchdf()
    except Exception as error:
        print(f"{symbol}: {error}", flush=True)
        return None
    finally:
        con.close()
    if len(bars) < 200:
        return None

    h = bars["high"].to_numpy(float)
    c = bars["close"].to_numpy(float)
    prev_c = np.roll(c, 1)
    prev_c[0] = c[0]
    low_arr = bars["low"].to_numpy(float)
    tr = np.maximum(h - low_arr, np.maximum(np.abs(h - prev_c), np.abs(low_arr - prev_c)))
    atr = pd.Series(tr).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean().to_numpy()
    atr_ratio = atr / np.maximum(c, 1e-12)
    atr_mult = atr_ratio / pd.Series(atr_ratio).rolling(30, min_periods=30).median().to_numpy()
    tp = (h + low_arr + c) / 3.0
    sma = pd.Series(tp).rolling(20, min_periods=20).mean()
    std = pd.Series(tp).rolling(20, min_periods=20).std(ddof=1)
    bbw = (4 * std / sma).to_numpy()
    bbw_mult = bbw / pd.Series(bbw).rolling(30, min_periods=30).median().to_numpy()
    wick = (h / np.maximum(c, 1e-12) - 1.0) * 100.0
    spike = h >= prev_c * SPIKE_RATIO

    valid = (
        np.isfinite(atr_mult) & np.isfinite(bbw_mult) & np.isfinite(wick) & np.isfinite(c)
    )
    timestamps = bars["open_ms"].to_numpy(np.int64)
    cutoff_ms = int(TRAIN_END.timestamp() * 1_000)
    test_start_ms = int(TEST_START.timestamp() * 1_000)
    segment = np.where(
        timestamps < cutoff_ms,
        "train",
        np.where(timestamps >= test_start_ms, "test", "embargo"),
    )

    rows: list[dict[str, object]] = []
    conditions: list[tuple[str, float, float, np.ndarray]] = [
        ("atr_wick", a, w, valid & (atr_mult > a) & (wick > w))
        for a in ATR_MULT_GRID
        for w in WICK_GRID
    ]
    conditions.append(("bbw_only", -1.0, -1.0, valid & (bbw_mult > BBW_THRESHOLD)))
    conditions.append(("background", 0.0, 0.0, valid))

    for name, a_value, w_value, cond in conditions:
        for seg in ("train", "test"):
            seg_mask = segment == seg
            mask = cond & seg_mask
            n = int(mask.sum())
            base_n = int((valid & seg_mask).sum())
            for horizon in HORIZONS:
                future = _future_spike_mask(spike, horizon)
                rows.append({
                    "symbol": symbol,
                    "condition": name,
                    "atr_mult": a_value,
                    "wick": w_value,
                    "horizon": horizon,
                    "segment": seg,
                    "n": n,
                    "hits": int((mask & future).sum()),
                    "spikes": int((seg_mask & spike).sum()),
                    "bars": base_n,
                })

    detail_mask = valid & (segment == "test")
    detail = pd.DataFrame({
        "symbol": symbol,
        "open_ms": timestamps[detail_mask],
        "atr_mult": atr_mult[detail_mask],
        "bbw_mult": bbw_mult[detail_mask],
        "wick": wick[detail_mask],
        "close": c[detail_mask],
    })
    return {"rows": rows, "detail": detail}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=10)
    parser.add_argument("--out", type=Path, default=Path("docs/research/PRE_SPIKE_STUDY_W3.md"))
    args = parser.parse_args(argv)

    started = time.monotonic()
    symbols = sorted({p.parents[4].name for p in ROOT.glob("*/1m/2026/**/*.parquet")})
    print(f"symbols: {len(symbols)}", flush=True)

    rows: list[dict[str, object]] = []
    details: list[pd.DataFrame] = []
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        for result in pool.map(_process_symbol, symbols, chunksize=8):
            rows.extend(result["rows"])
            details.append(result["detail"])

    grid = pd.DataFrame(rows)
    grid.to_parquet("/tmp/opencode/pre_spike_grid_raw.parquet", index=False)
    agg = grid.groupby(["condition", "atr_mult", "wick", "horizon", "segment"], as_index=False).agg(
        n=("n", "sum"), hits=("hits", "sum"), spikes=("spikes", "sum"), bars=("bars", "sum")
    )
    agg["prob"] = agg["hits"] / agg["n"].replace(0, np.nan)
    agg["base_prob"] = agg["spikes"] / agg["bars"].replace(0, np.nan)
    agg["lift"] = agg["prob"] / agg["base_prob"]

    detail_all = pd.concat(details, ignore_index=True)
    detail_all.to_parquet("/tmp/opencode/pre_spike_test_detail.parquet", index=False)

    lines: list[str] = [
        "# Pre-Spike 前兆预警研究（W3）", "",
        f"- symbols: {len(symbols)}，1m 全历史",
        f"- train < {TRAIN_END.date()} / test >= {TEST_START.date()}（embargo 1h）",
        f"- spike 定义: high ≥ prev_close × {SPIKE_RATIO}；标签不含当前 bar",
        f"- 条件网格: atr_mult ∈ {ATR_MULT_GRID} × wick ∈ {WICK_GRID}%，对照 bbw>{BBW_THRESHOLD}",
        "",
        "## 背景 spike 概率（无条件）", "",
        agg[agg["condition"] == "background"][["horizon", "segment", "bars", "spikes", "base_prob"]]
        .rename(columns={"base_prob": "prob"})
        .to_string(index=False), "",
        "## Train lift（选参段）", "",
        agg[(agg["segment"] == "train") & (agg["condition"] != "background")]
        .pivot_table(index=["condition", "atr_mult", "wick"], columns="horizon", values="lift")
        .round(2).to_string(), "",
        "## Test lift（验证段）", "",
        agg[(agg["segment"] == "test") & (agg["condition"] != "background")]
        .pivot_table(index=["condition", "atr_mult", "wick"], columns="horizon", values="lift")
        .round(2).to_string(), "",
        "## Test 触发次数", "",
        agg[(agg["segment"] == "test") & (agg["condition"] != "background")]
        .pivot_table(index=["condition", "atr_mult", "wick"], columns="horizon", values="n")
        .to_string(), "",
    ]

    stage2_path = Path("/tmp/opencode/pre_spike_stage2.py")
    print(f"网格完成 耗时{time.monotonic()-started:.0f}s；明细 {len(detail_all)} 行")
    args.out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"报告骨架: {args.out}（stage2 成本模拟脚本待生成: {stage2_path.name}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
