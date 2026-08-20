"""统计上涨插针后的 OI 变化分档及后续涨跌概率（按用户定义的时间对齐）。

时间对齐规则（5m OI）：
- 基准 OI 点 = 插针(high_utc) 前最近一个 available_time <= 插针时刻 的 5m 点（如 30:00）
- 后续 3 个 OI 点 = 基准之后第 1/2/3 个 5m 点（如 35:00 / 40:00 / 45:00）
- d_oi_k = (第k个点 OI - 基准 OI) / 基准 OI × 100

数据源: reports/amplitude/daily_amplitude_cycles_spike_up.csv（全量 spike_up 周期，非回测币种）
价格基准: 插针 bar（high_utc 所在 1m）收盘价
涨跌: 插针后 15/30/60 分钟收盘 vs 插针 bar 收盘
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


def load_px(symbol: str) -> pd.DataFrame:
    if symbol in _cache_px:
        return _cache_px[symbol]
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
    _cache_px[symbol] = df
    return df


def main() -> None:
    df = pd.read_csv(CSV_PATH)
    df["high_ms"] = pd.to_datetime(df["high_utc"], utc=True).astype("int64") // 1_000
    print(f"spike_up 周期总数: {len(df)}")

    rows = []
    for r in df.itertuples():
        oi_df = load_oi(r.symbol)
        px_df = load_px(r.symbol)
        if oi_df.empty or px_df.empty:
            continue
        high_ms = int(r.high_ms)

        # 插针 bar 收盘价作为价格基准
        px_at = px_df[px_df["ms"] == high_ms]
        if px_at.empty:
            continue
        base_price = float(px_at.iloc[0]["close"])

        # 基准 OI 点 = 插针前最近 available_time <= high_ms 的 5m 点
        prev_oi = oi_df[oi_df["ms"] <= high_ms]
        next_oi = oi_df[oi_df["ms"] > high_ms]
        if prev_oi.empty or len(next_oi) < 3:
            continue
        base_ms = int(prev_oi.iloc[-1]["ms"])
        base_oi = float(prev_oi.iloc[-1]["oi"])
        if base_oi <= 0:
            continue

        oi_row = {"base_ms": base_ms, "base_oi": base_oi}
        ok = True
        for k in (1, 2, 3):
            pt = next_oi.iloc[k - 1]
            oi_row[f"oi_{k}_ms"] = int(pt["ms"])
            oi_row[f"d_oi_{k}"] = (float(pt["oi"]) - base_oi) / base_oi * 100.0

        px_after = px_df[px_df["ms"] > high_ms]
        px_row = {}
        for k, lab in ((15, "15"), (30, "30"), (60, "60")):
            pt = px_after.iloc[k - 1] if len(px_after) >= k else None
            px_row[f"ret_{lab}m"] = (
                (float(pt["close"]) - base_price) / base_price * 100.0
                if pt is not None else None
            )

        rows.append({
            "symbol": r.symbol,
            "high_utc": r.high_utc,
            "high_price": float(r.high_price),
            "base_price": base_price,
            **oi_row,
            **px_row,
        })
    out = pd.DataFrame(rows)
    print(f"有效样本: {len(out)}")
    out.to_csv("reports/oi_spikeup_3point.csv", index=False)

    bands = [
        ("OI降>15%", None, -15),
        ("OI降10-15%", -15, -10),
        ("OI降5-10%", -10, -5),
        ("OI降3-5%", -5, -3),
        ("OI降0-3%", -3, 0),
        ("OI升0-3%", 0, 3),
        ("OI升3-5%", 3, 5),
        ("OI升5-10%", 5, 10),
        ("OI升>10%", 10, None),
    ]

    for k, oi_lab in ((1, "35分点"), (2, "40分点"), (3, "45分点")):
        col = f"d_oi_{k}"
        print(f"\n=== 档位: 插针后{oi_lab}(相对基准{col}) OI 变化 ===")
        print(f"{'档位':<12} {'n':>5} | {'+15m涨':>7} {'+30m涨':>7} {'+60m涨':>7} | "
              f"{'15m收益中位':>10} {'30m收益中位':>10} {'60m收益中位':>10}")
        for label, lo, hi in bands:
            if lo is None:
                cond = out[col] < hi
            elif hi is None:
                cond = out[col] >= lo
            else:
                cond = (out[col] >= lo) & (out[col] < hi)
            d = out[cond]
            if len(d) < 3:
                continue
            r15, r30, r60 = d["ret_15m"].dropna(), d["ret_30m"].dropna(), d["ret_60m"].dropna()
            print(f"{label:<12} {len(d):>5} | {(r15 > 0).mean():>6.0%} {(r30 > 0).mean():>6.0%} "
                  f"{(r60 > 0).mean():>6.0%} | {r15.median():>+9.2f}% {r30.median():>+9.2f}% "
                  f"{r60.median():>+9.2f}%")

    print("\n=== 汇总对比: OI降>5% vs OI未降 ===")
    for k, oi_lab in ((1, "35分点"), (2, "40分点"), (3, "45分点")):
        col = f"d_oi_{k}"
        big = out[out[col] < -5]
        rest = out[out[col] >= -5]
        for lab, d in ((f"降>5%@{oi_lab}", big), (f"未降5%@{oi_lab}", rest)):
            r15 = d["ret_15m"].dropna()
            r30 = d["ret_30m"].dropna()
            r60 = d["ret_60m"].dropna()
            print(f"  {lab:<14} n={len(d):>4} +15m涨{(r15 > 0).mean():.0%} "
                  f"+30m涨{(r30 > 0).mean():.0%} +60m涨{(r60 > 0).mean():.0%} "
                  f"| 中位 {r15.median():+.2f}%")


if __name__ == "__main__":
    main()