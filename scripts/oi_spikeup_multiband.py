"""统计上涨插针(最高价)后的 OI 变化分档及后续价格涨跌概率。

数据源: reports/amplitude/daily_amplitude_cycles_spike_up.csv（全量 spike_up 周期）
基准点: high_utc（wave 内最高价时刻，1m bar 起点 = 插针顶点）
OI: data/market/metrics 5m 粒度，插针前最近 5m 点为基准
涨跌: 插针后 15/30/60 分钟收盘价 vs 插针最高价
"""
from __future__ import annotations

from pathlib import Path

import duckdb
import pandas as pd

CSV_PATH = Path("reports/amplitude/daily_amplitude_cycles_spike_up.csv")
METRICS_ROOT = Path("data/market/metrics")
CANDLES_ROOT = Path("data/market/candles")

_cache_oi: dict[str, pd.DataFrame] = {}
_cache_price: dict[str, pd.DataFrame] = {}


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


def load_price(symbol: str) -> pd.DataFrame:
    if symbol in _cache_price:
        return _cache_price[symbol]
    paths = sorted(str(p) for p in CANDLES_ROOT.glob(f"{symbol}/1m/**/*.parquet"))
    df = pd.DataFrame(columns=["ms", "close"])
    if paths:
        con = duckdb.connect()
        try:
            rows = con.execute(
                """
                SELECT epoch_ms(open_time) AS ms, CAST(close AS DOUBLE) AS close
                FROM read_parquet(?)
                WHERE symbol = ? ORDER BY open_time
                """,
                [paths, symbol],
            ).fetchall()
        finally:
            con.close()
        df = pd.DataFrame(rows, columns=["ms", "close"])
    df = df.drop_duplicates("ms").sort_values("ms").reset_index(drop=True)
    _cache_price[symbol] = df
    return df


def main() -> None:
    df = pd.read_csv(CSV_PATH)
    df["high_ms"] = pd.to_datetime(df["high_utc"], utc=True).astype("int64") // 1_000
    print(f"spike_up 周期总数: {len(df)}")

    rows = []
    missing_oi = missing_px = 0
    for r in df.itertuples():
        oi_df = load_oi(r.symbol)
        px_df = load_price(r.symbol)
        if oi_df.empty:
            missing_oi += 1
            continue
        if px_df.empty:
            missing_px += 1
            continue
        high_ms = int(r.high_ms)
        high_price = float(r.high_price)

        # 基准价: 插针 bar (high_utc 所在 1m) 的收盘价
        px_at = px_df[px_df["ms"] == high_ms]
        if px_at.empty:
            continue
        base_price = float(px_at.iloc[0]["close"])

        # OI: 插针前最近 5m 点为基准
        before = oi_df[oi_df["ms"] <= high_ms]
        if before.empty:
            continue
        base_oi = float(before.iloc[-1]["oi"])
        if base_oi <= 0:
            continue
        oi_after = oi_df[oi_df["ms"] > high_ms]
        oi_row = {"base_oi": base_oi}
        for k in (1, 2, 3):
            if len(oi_after) >= k:
                oi_row[f"d_oi_{k}"] = (float(oi_after.iloc[k - 1]["oi"]) - base_oi) / base_oi * 100.0
            else:
                oi_row[f"d_oi_{k}"] = None
        if oi_row["d_oi_1"] is None:
            continue

        # 价格: 插针 bar 收盘后 15/30/60 分钟收盘 vs 插针 bar 收盘
        px_after = px_df[px_df["ms"] > high_ms]
        px_row = {}
        for k, lab in ((15, "15"), (30, "30"), (60, "60")):
            pt = px_after.iloc[k - 1] if len(px_after) >= k else None
            if pt is None:
                px_row[f"ret_{lab}m"] = None
            else:
                px_row[f"ret_{lab}m"] = (float(pt["close"]) - base_price) / base_price * 100.0

        rows.append({
            "symbol": r.symbol,
            "high_utc": r.high_utc,
            "high_price": high_price,
            "base_price": base_price,
            "drawdown_pct": (float(r.end_price) - high_price) / high_price * 100.0,
            **oi_row,
            **px_row,
        })
    out = pd.DataFrame(rows)
    print(f"有效样本: {len(out)} (缺OI {missing_oi}, 缺价 {missing_px})")
    out.to_csv("reports/oi_spikeup_multiband.csv", index=False)

    print("\n=== 插针后 OI 变化分档 (d_oi_1: 插针后第1根5m, 即+5m) ===")
    print("档位                n   | +15m涨  +30m涨  +60m涨  | 15m回撤中位  OI后续中位(d_oi_2)")
    bands = [
        ("OI降>15%", out["d_oi_1"] < -15),
        ("OI降10-15%", (out["d_oi_1"] <= -10) & (out["d_oi_1"] > -15)),
        ("OI降5-10%", (out["d_oi_1"] <= -5) & (out["d_oi_1"] > -10)),
        ("OI降3-5%", (out["d_oi_1"] <= -3) & (out["d_oi_1"] > -5)),
        ("OI降0-3%", (out["d_oi_1"] <= 0) & (out["d_oi_1"] > -3)),
        ("OI升0-3%", (out["d_oi_1"] > 0) & (out["d_oi_1"] <= 3)),
        ("OI升3-5%", (out["d_oi_1"] > 3) & (out["d_oi_1"] <= 5)),
        ("OI升5-10%", (out["d_oi_1"] > 5) & (out["d_oi_1"] <= 10)),
        ("OI升>10%", out["d_oi_1"] > 10),
    ]
    for label, cond in bands:
        d = out[cond]
        if len(d) < 3:
            continue
        r15 = d["ret_15m"].dropna()
        r30 = d["ret_30m"].dropna()
        r60 = d["ret_60m"].dropna()
        print(
            f"{label:<12} {len(d):>4} | "
            f"{(r15 > 0).mean():.0%}    {(r30 > 0).mean():.0%}    {(r60 > 0).mean():.0%}   | "
            f"{d['drawdown_pct'].median():+.2f}%      {d['d_oi_2'].median():+.2f}%"
        )

    print("\n=== 插针后最大 OI 降幅档位 (+15m窗口) ===")
    out["min_d"] = out[["d_oi_1", "d_oi_2", "d_oi_3"]].min(axis=1)
    for label, cond in [
        ("max降>15%", out["min_d"] < -15),
        ("max降10-15%", (out["min_d"] <= -10) & (out["min_d"] > -15)),
        ("max降5-10%", (out["min_d"] <= -5) & (out["min_d"] > -10)),
        ("max降0-5%", (out["min_d"] <= 0) & (out["min_d"] > -5)),
        ("OI始终升", out["min_d"] > 0),
    ]:
        d = out[cond]
        if len(d) < 3:
            continue
        r15 = d["ret_15m"].dropna()
        r30 = d["ret_30m"].dropna()
        r60 = d["ret_60m"].dropna()
        print(
            f"{label:<14} {len(d):>4} | "
            f"{(r15 > 0).mean():.0%}    {(r30 > 0).mean():.0%}    {(r60 > 0).mean():.0%}   | "
            f"15m回撤中位 {d['drawdown_pct'].median():+.2f}%"
        )

    print("\n=== 汇总: 全量基准 ===")
    for lab, col in (("15m", "ret_15m"), ("30m", "ret_30m"), ("60m", "ret_60m")):
        s = out[col].dropna()
        print(f"  {lab}: 上涨概率 {(s > 0).mean():.0%} 中位收益 {s.median():+.2f}%")


if __name__ == "__main__":
    main()