"""OI × 量价交叉分析：插针后 OI 变化 × 成交量变化 → 后续涨跌概率。

四象限(经典量仓分析):
- 价涨+OI增 = 新多进场(趋势延续)
- 价涨+OI减 = 空头回补/爆仓(可能反转)
- 价跌+OI增 = 新空进场
- 价跌+OI减 = 多头离场/爆仓

数据源: reports/amplitude/daily_amplitude_cycles_spike_up.csv
基准: 插针(high_utc)前最近 5m OI 点, 后续 35/40/45 分 OI 点
成交量: 1m 聚合到 5m 窗口对比
"""
from __future__ import annotations

from pathlib import Path

import duckdb
import pandas as pd

CSV_PATH = Path("reports/amplitude/daily_amplitude_cycles_spike_up.csv")
METRICS_ROOT = Path("data/market/metrics")
CANDLES_ROOT = Path("data/market/candles")

_cache_oi: dict[str, pd.DataFrame] = {}
_cache_px: dict[str, pd.DataFrame] = {}
_cache_vol: dict[str, pd.DataFrame] = {}


def load_oi(symbol: str) -> pd.DataFrame:
    if symbol in _cache_oi:
        return _cache_oi[symbol]
    paths = sorted(str(p) for p in METRICS_ROOT.glob(f"{symbol}/**/*.parquet"))
    df = pd.DataFrame(columns=["ms", "oi"])
    if paths:
        con = duckdb.connect()
        try:
            rows = con.execute(
                """
                SELECT extract(epoch from available_time AT TIME ZONE 'UTC') * 1000 AS ms,
                       sum_open_interest AS oi
                FROM read_parquet(?)
                WHERE symbol = ? AND period = '5m' AND sum_open_interest IS NOT NULL
                ORDER BY available_time
                """,
                [paths, symbol],
            ).fetchall()
        finally:
            con.close()
        df = pd.DataFrame(rows, columns=["ms", "oi"])
    df = df.drop_duplicates("ms").sort_values("ms").reset_index(drop=True)
    _cache_oi[symbol] = df
    return df


def load_px_vol(symbol: str) -> pd.DataFrame:
    if symbol in _cache_px:
        return _cache_px[symbol]
    paths = sorted(str(p) for p in CANDLES_ROOT.glob(f"{symbol}/1m/**/*.parquet"))
    df = pd.DataFrame(columns=["ms", "close", "volume"])
    if paths:
        con = duckdb.connect()
        try:
            rows = con.execute(
                """
                SELECT epoch_ms(open_time) AS ms, CAST(close AS DOUBLE) AS close,
                       CAST(volume AS DOUBLE) AS volume
                FROM read_parquet(?)
                WHERE symbol = ? ORDER BY open_time
                """,
                [paths, symbol],
            ).fetchall()
        finally:
            con.close()
        df = pd.DataFrame(rows, columns=["ms", "close", "volume"])
    df = df.drop_duplicates("ms").sort_values("ms").reset_index(drop=True)
    _cache_px[symbol] = df
    return df


def main() -> None:
    df = pd.read_csv(CSV_PATH)
    df["high_ms"] = pd.to_datetime(df["high_utc"], utc=True).astype("int64") // 1_000
    print(f"spike_up 周期总数: {len(df)}")

    rows = []
    for r in df.itertuples():
        oi_df = load_oi(r.symbol)
        px_df = load_px_vol(r.symbol)
        if oi_df.empty or px_df.empty:
            continue
        high_ms = int(r.high_ms)

        px_at = px_df[px_df["ms"] == high_ms]
        if px_at.empty:
            continue
        base_price = float(px_at.iloc[0]["close"])

        prev_oi = oi_df[oi_df["ms"] <= high_ms]
        next_oi = oi_df[oi_df["ms"] > high_ms]
        if prev_oi.empty or len(next_oi) < 3:
            continue
        base_oi = float(prev_oi.iloc[-1]["oi"])
        if base_oi <= 0:
            continue
        base_ms = int(prev_oi.iloc[-1]["ms"])

        d_oi = (float(next_oi.iloc[0]["oi"]) - base_oi) / base_oi * 100.0

        # 成交量: 插针 bar 前 5 分钟 vs 后 5 分钟
        vol_before = px_df[(px_df["ms"] > base_ms - 300_000) & (px_df["ms"] <= base_ms)]
        vol_after = px_df[(px_df["ms"] > high_ms) & (px_df["ms"] <= high_ms + 300_000)]
        vol_ratio = None
        if len(vol_before) >= 3 and len(vol_after) >= 3:
            vb = float(vol_before["volume"].sum())
            va = float(vol_after["volume"].sum())
            if vb > 0:
                vol_ratio = va / vb

        px_after = px_df[px_df["ms"] > high_ms]
        r15 = None
        if len(px_after) >= 15:
            r15 = (float(px_after.iloc[14]["close"]) - base_price) / base_price * 100.0
        r60 = None
        if len(px_after) >= 60:
            r60 = (float(px_after.iloc[59]["close"]) - base_price) / base_price * 100.0

        rows.append({
            "symbol": r.symbol, "high_utc": r.high_utc,
            "d_oi": d_oi, "vol_ratio": vol_ratio,
            "ret_15m": r15, "ret_60m": r60,
        })
    out = pd.DataFrame(rows)
    print(f"有效样本: {len(out)}")
    out.to_csv("reports/oi_vol_cross.csv", index=False)

    q = out.dropna(subset=["d_oi", "vol_ratio", "ret_15m"])
    print(f"含成交量样本: {len(q)}")

    # 四象限: OI升/降 × 放量/缩量
    print("\n=== 四象限: 插针后第一个5m OI变化 × 插针后5分钟成交量 ===\n")
    print(f"{'象限':<28} {'n':>4} | {'+15m涨':>7} {'+60m涨':>7} | {'15m中位':>8} {'60m中位':>8}")
    quads = [
        ("OI升>5% + 放量>2x", (q.d_oi > 5) & (q.vol_ratio > 2)),
        ("OI升>5% + 温和(1-2x)", (q.d_oi > 5) & (q.vol_ratio > 1) & (q.vol_ratio <= 2)),
        ("OI升>5% + 缩量<1x", (q.d_oi > 5) & (q.vol_ratio <= 1)),
        ("OI升0-5% + 放量>2x", (q.d_oi > 0) & (q.d_oi <= 5) & (q.vol_ratio > 2)),
        ("OI升0-5% + 温和", (q.d_oi > 0) & (q.d_oi <= 5) & (q.vol_ratio > 1) & (q.vol_ratio <= 2)),
        ("OI升0-5% + 缩量", (q.d_oi > 0) & (q.d_oi <= 5) & (q.vol_ratio <= 1)),
        ("OI降0-5% + 放量>2x", (q.d_oi <= 0) & (q.d_oi > -5) & (q.vol_ratio > 2)),
        ("OI降0-5% + 温和", (q.d_oi <= 0) & (q.d_oi > -5) & (q.vol_ratio > 1) & (q.vol_ratio <= 2)),
        ("OI降0-5% + 缩量", (q.d_oi <= 0) & (q.d_oi > -5) & (q.vol_ratio <= 1)),
        ("OI降>5% + 放量>2x", (q.d_oi <= -5) & (q.vol_ratio > 2)),
        ("OI降>5% + 温和", (q.d_oi <= -5) & (q.vol_ratio > 1) & (q.vol_ratio <= 2)),
        ("OI降>5% + 缩量", (q.d_oi <= -5) & (q.vol_ratio <= 1)),
    ]
    for label, cond in quads:
        d = q[cond]
        if len(d) < 5:
            print(f"{label:<28} {len(d):>4} (样本少)")
            continue
        r15, r60 = d["ret_15m"].dropna(), d["ret_60m"].dropna()
        print(f"{label:<28} {len(d):>4} | {(r15 > 0).mean():>6.0%} {(r60 > 0).mean():>6.0%} | "
              f"{r15.median():>+7.2f}% {r60.median():>+7.2f}%")

    # 相关性
    print("\n=== 相关性 ===")
    print(f"d_oi vs vol_ratio: {q.d_oi.corr(q.vol_ratio):+.3f}")
    print(f"d_oi vs ret_15m: {q.d_oi.corr(q.ret_15m):+.3f}")
    print(f"vol_ratio vs ret_15m: {q.vol_ratio.corr(q.ret_15m):+.3f}")
    print(f"d_oi vs ret_60m: {q.d_oi.corr(q.ret_60m):+.3f}")
    print(f"vol_ratio vs ret_60m: {q.vol_ratio.corr(q.ret_60m):+.3f}")


if __name__ == "__main__":
    main()