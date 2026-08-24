"""W6: 衍生品慢因子与 市场相对 因子对前兆预警的增量检验。

在前兆命中时刻（atr>5x & wick>4%）上叠加:
  - oi_z1h:      OI 变化 1 小时 Z-score（杠杆堆积）
  - lsr_z24h:    全市场多空比 24h Z-score（拥挤度）
  - market_rel_5m:  5 分钟相对 BTC 收益（剥离市场 beta）
标签: 未来 20 根 1m 内 spike>=15%（不含当前 bar）。
metrics 按 available_time 因果拼接；背景 bar 按 1/50 抽样作对照。
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
METRICS_ROOT = Path("data/market/metrics")
TRAIN_END = pd.Timestamp("2026-04-30 23:00:00+00:00")
TEST_START = pd.Timestamp("2026-05-01 00:00:00+00:00")
WARN_ATR = 5.0
WARN_WICK = 4.0
SPIKE_RATIO = 1.15
BACKGROUND_SAMPLE = 50
_BENCH_EXCLUDE = frozenset({"BTCUSDT", "ETHUSDT"})


def _future_spike(h: np.ndarray, c: np.ndarray, horizon: int) -> np.ndarray:
    series = pd.Series((h >= np.roll(c, 1) * SPIKE_RATIO).astype(float))
    series.iloc[0] = float(h[0] >= c[0] * SPIKE_RATIO)
    forward = (
        series.iloc[::-1].rolling(horizon + 1, min_periods=horizon + 1).sum().iloc[::-1].to_numpy()
    )
    return np.nan_to_num(forward - series.to_numpy(), nan=0.0) > 0


def load_metrics(symbol: str, start_ms: int) -> pd.DataFrame | None:
    paths = sorted(METRICS_ROOT.glob(f"{symbol}/2026/**/*.parquet")) + sorted(
        METRICS_ROOT.glob(f"{symbol}/2025/**/*.parquet")
    )
    if not paths:
        return None
    con = duckdb.connect()
    try:
        df = con.execute(
            f"""SELECT epoch_ms(snapshot_time)::BIGINT AS snapshot_ms,
                       epoch_ms(available_time)::BIGINT AS available_ms,
                       sum_open_interest, count_long_short_ratio,
                       sum_taker_long_short_vol_ratio
                FROM read_parquet({[str(p) for p in paths]!r}, union_by_name=true)
                WHERE available_ms >= ? ORDER BY available_ms""",
            [start_ms],
        ).fetchdf()
    except Exception:
        return None
    finally:
        con.close()
    return df if not df.empty else None


def process_symbol(payload: tuple[str, np.ndarray, np.ndarray]) -> pd.DataFrame | None:
    symbol, bench_index, bench_ret = payload
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
    if len(bars) < 1000 or "BTCUSDT" == symbol:
        return None

    h = bars["high"].to_numpy(float)
    c = bars["close"].to_numpy(float)
    prev_c = np.roll(c, 1)
    prev_c[0] = c[0]
    tr = np.maximum(h - bars["low"].to_numpy(float), np.maximum(np.abs(h - prev_c), np.abs(bars["low"].to_numpy(float) - prev_c)))
    atr = pd.Series(tr).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean().to_numpy()
    atr_ratio = atr / np.maximum(c, 1e-12)
    atr_mult = atr_ratio / pd.Series(atr_ratio).rolling(30, min_periods=30).median().to_numpy()
    wick = (h / np.maximum(c, 1e-12) - 1.0) * 100.0
    times = bars["open_ms"].to_numpy(np.int64)
    close_series = pd.Series(c, index=times)
    ret5 = close_series.pct_change(5).reindex(times).to_numpy()

    common = sorted(set(close_series.index).intersection(bench_index))
    bench_aligned = pd.Series(bench_ret, index=bench_index).reindex(common)
    sym_aligned = close_series.reindex(common).pct_change(5)
    market_rel = (sym_aligned - bench_aligned).reindex(times).to_numpy()

    spike20 = _future_spike(h, c, 20)

    cutoff_ms = int(TRAIN_END.timestamp() * 1_000)
    test_start_ms = int(TEST_START.timestamp() * 1_000)
    segment = np.where(times < cutoff_ms, "train", np.where(times >= test_start_ms, "test", "embargo"))

    warn = (atr_mult > WARN_ATR) & (wick > WARN_WICK)
    keep = warn | (np.arange(len(times)) % BACKGROUND_SAMPLE == 0)
    frame = pd.DataFrame({
        "symbol": symbol,
        "open_ms": times[keep],
        "segment": segment[keep],
        "warn": warn[keep],
        "market_rel_5m": market_rel[keep],
        "spike20": spike20[keep],
    })

    metrics = load_metrics(symbol, int(times[0]))
    if metrics is not None and len(metrics) > 20:
        merged = pd.merge_asof(
            frame.sort_values("open_ms"),
            metrics[["available_ms", "sum_open_interest", "count_long_short_ratio"]].sort_values("available_ms"),
            left_on="open_ms",
            right_on="available_ms",
            direction="backward",
        )
        oi = pd.to_numeric(merged["sum_open_interest"], errors="coerce")
        lsr = pd.to_numeric(merged["count_long_short_ratio"], errors="coerce")
        merged["oi_chg_60m"] = oi.pct_change(12)
        merged["oi_z1h"] = (merged["oi_chg_60m"] - merged["oi_chg_60m"].rolling(288, min_periods=100).mean()) / merged["oi_chg_60m"].rolling(288, min_periods=100).std()
        merged["lsr_z24h"] = (lsr - lsr.rolling(288, min_periods=100).mean()) / lsr.rolling(288, min_periods=100).std()
        frame = merged[["symbol", "open_ms", "segment", "warn", "market_rel_5m", "spike20", "oi_z1h", "lsr_z24h"]]
    else:
        frame["oi_z1h"] = np.nan
        frame["lsr_z24h"] = np.nan
    return frame


def _load_market_benchmark(top_n: int = 20) -> tuple[np.ndarray, np.ndarray]:
    """等权市场基准：1m 归档最全的 top_n symbol 每分钟中位收益。"""
    con = duckdb.connect()
    idx = con.execute(
        "SELECT symbol, count(*) files FROM read_parquet('data/market/candles/archive_index.parquet') "
        "WHERE timeframe='1m' GROUP BY symbol ORDER BY files DESC LIMIT ?",
        [top_n],
    ).fetchdf()
    series_map: dict[str, pd.Series] = {}
    for symbol in idx["symbol"]:
        path = ROOT / symbol / "1m" / "**" / "*.parquet"
        try:
            df = con.execute(
                """SELECT epoch_ms(open_time)::BIGINT AS open_ms, close
                   FROM read_parquet(?, union_by_name=true) ORDER BY open_time""",
                [str(path)],
            ).fetchdf()
        except Exception:
            continue
        if len(df) > 100000:
            series_map[symbol] = pd.Series(
                df["close"].to_numpy(float), index=df["open_ms"].to_numpy(np.int64)
            )
    con.close()
    panel = pd.DataFrame(series_map)
    market_median = panel.median(axis=1)
    index = market_median.index.to_numpy(np.int64)
    ret = market_median.pct_change().to_numpy()
    print(f"benchmark symbols: {len(series_map)}", flush=True)
    return index, ret


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=10)
    args = parser.parse_args(argv)
    started = time.monotonic()

    bench_index, bench_ret = _load_market_benchmark()
    symbols = sorted({p.parents[4].name for p in ROOT.glob("*/1m/2026/**/*.parquet")} - set(_BENCH_EXCLUDE))
    print(f"symbols: {len(symbols)}", flush=True)

    parts: list[pd.DataFrame] = []
    payloads = [(s, bench_index, bench_ret) for s in symbols]
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        for frame in pool.map(process_symbol, payloads, chunksize=8):
            if frame is not None and not frame.empty:
                parts.append(frame)
    data = pd.concat(parts, ignore_index=True)
    data.to_parquet("/tmp/opencode/w6_factors.parquet", index=False)
    print(f"rows={len(data)} warn_hits={int(data['warn'].sum())} 耗时{time.monotonic()-started:.0f}s", flush=True)

    lines = [
        "# W6: 衍生品慢因子与 市场相对 对前兆预警的增量", "",
        f"- train < {TRAIN_END.date()} / test >= {TEST_START.date()}；背景 bar 按 1/{BACKGROUND_SAMPLE} 抽样",
        "- 基础规则: `atr>5x & wick>4%`；标签: 未来 20 根内 spike≥15%",
        "",
    ]

    def lift_table(sub: pd.DataFrame, factor: str, bins: tuple[float, ...]) -> str:
        rows = []
        for seg in ("train", "test"):
            seg_df = sub[sub["segment"] == seg]
            base = seg_df["spike20"].mean()
            for i, label in enumerate(_bin_labels(bins)):
                lo, hi = bins[i], bins[i + 1]
                cell = seg_df[(seg_df[factor] > lo) & (seg_df[factor] <= hi)]
                prob = cell["spike20"].mean() if len(cell) else np.nan
                rows.append({"segment": seg, "bucket": label, "n": len(cell), "prob": round(prob, 4) if np.isfinite(prob) else np.nan, "lift": round(prob / base, 1) if np.isfinite(prob) and base > 0 else np.nan})
        return pd.DataFrame(rows).to_string(index=False)

    def _bin_labels(bins: tuple[float, ...]) -> list[str]:
        labels = []
        for lo, hi in zip(bins[:-1], bins[1:]):
            labels.append(f"({lo:g},{hi:g}]")
        return labels

    warn_data = data[data["warn"]]
    for factor, bins in (("oi_z1h", (-10, -0.5, 0.5, 10)), ("lsr_z24h", (-10, -0.5, 0.5, 10)), ("market_rel_5m", (-1, 0.0, 0.02, 1))):
        lines += [f"## {factor} 分层（仅前兆命中样本）", "", lift_table(warn_data, factor, bins), ""]
    lines += ["## market_rel 分层（全部 bar 含背景）", "", lift_table(data.dropna(subset=["market_rel_5m"]), "market_rel_5m", (-1, -0.01, 0.0, 0.02, 1)), ""]

    out = Path("docs/research/PRE_SPIKE_STUDY_W6.md")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"报告: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
