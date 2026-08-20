"""验证上涨插针前后 OI 变化：真插针(爆仓)是否伴随 OI 大幅下降。

对基线全部触发信号，取插针时刻前后 5m 粒度的 OI 序列，
统计插针后 N 个 5m 窗口的 OI 变化率分布。
"""
from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pandas as pd

SIGNALS_CSV = Path("reports/spike-v2.2-db-full-reject-only/all_signals.csv")
METRICS_ROOT = Path("data/market/metrics")


def load_oi_series(symbol: str) -> pd.DataFrame:
    paths = sorted(str(p) for p in METRICS_ROOT.glob(f"{symbol}/**/*.parquet"))
    if not paths:
        return pd.DataFrame(columns=["available_time", "oi"])
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
    return df


def analyze_signal(signal_time_ms: int, oi_df: pd.DataFrame) -> dict | None:
    if oi_df.empty:
        return None
    # 插针时刻前最近的 5m 点（视为插针前 OI 基线）
    before = oi_df[oi_df["ms"] <= signal_time_ms]
    after = oi_df[oi_df["ms"] > signal_time_ms]
    if before.empty or after.empty:
        return None
    base_ms, base_oi = before.iloc[-1]
    if base_oi <= 0:
        return None
    row = {"base_ms": base_ms, "base_oi": base_oi}
    for k in (1, 2, 3, 6, 12):
        pt = after.iloc[k - 1] if len(after) >= k else None
        if pt is None:
            row[f"oi_{k}"] = None
            row[f"d_oi_{k}"] = None
            continue
        row[f"oi_{k}"] = pt["oi"]
        row[f"d_oi_{k}"] = (pt["oi"] - base_oi) / base_oi * 100.0
    row["gap_ms"] = after.iloc[0]["ms"] - signal_time_ms
    return row


def main() -> None:
    sig = pd.read_csv(SIGNALS_CSV)
    trig = sig[sig["event_type"] == "signal_triggered"]
    print(f"触发信号总数: {len(trig)}")

    rows = []
    missing = 0
    for r in trig.itertuples():
        oi_df = load_oi_series(r.symbol)
        if oi_df.empty:
            missing += 1
            continue
        row = analyze_signal(int(r.event_time), oi_df)
        if row is None:
            missing += 1
            continue
        row["symbol"] = r.symbol
        row["signal_time"] = int(r.event_time)
        rows.append(row)
    df = pd.DataFrame(rows)
    print(f"有 OI 数据: {len(df)}, 缺失: {missing}")
    if df.empty:
        return
    out = Path("reports/oi_wick_analysis.csv")
    df.to_csv(out, index=False)
    print(f"保存: {out}")

    print("\n=== 插针后 OI 变化率分布 (%, 相对插针前最近 5m 点) ===")
    for k in (1, 2, 3, 6, 12):
        col = f"d_oi_{k}"
        s = df[col].dropna()
        if s.empty:
            print(f"  +{k}根5m: 无数据")
            continue
        down = (s < 0).sum()
        big_down = (s < -5).sum()
        big_down10 = (s < -10).sum()
        print(
            f"  +{k}根5m: n={len(s)} 中位={s.median():+.2f}% "
            f"下降={down/len(s):.0%} <-5%={big_down/len(s):.0%} "
            f"<-10%={big_down10/len(s):.0%}"
        )

    print("\n=== 插针后 OI 最大降幅（12 根 5m 窗口内） ===")
    cols = ["d_oi_1", "d_oi_2", "d_oi_3", "d_oi_6", "d_oi_12"]
    df["min_d_oi"] = df[cols].min(axis=1)
    s = df["min_d_oi"].dropna()
    print(f"  最大降幅: n={len(s)} 中位={s.median():+.2f}% "
          f"<-5%={(s<-5).mean():.0%} <-10%={(s<-10).mean():.0%} <-20%={(s<-20).mean():.0%}")


if __name__ == "__main__":
    main()