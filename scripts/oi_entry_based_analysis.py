"""以成交(entry)时刻为基准，验证插针后 OI 变化与最终盈亏/退出原因的关系。

关键问题：5m 粒度 OI 变化能否区分"真插针(应持有)"与"假插针/继续涨(应止损)"。
"""
from __future__ import annotations

from pathlib import Path

import duckdb
import pandas as pd

TRADES_CSV = Path("reports/spike-v2.2-db-full-reject-only/all_trades.csv")
METRICS_ROOT = Path("data/market/metrics")


def load_oi_series(symbol: str) -> pd.DataFrame:
    paths = sorted(str(p) for p in METRICS_ROOT.glob(f"{symbol}/**/*.parquet"))
    if not paths:
        return pd.DataFrame(columns=["ms", "oi"])
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
    return df.drop_duplicates("ms").sort_values("ms").reset_index(drop=True)


def oi_changes_after(entry_ms: int, oi_df: pd.DataFrame) -> dict:
    if oi_df.empty:
        return {}
    before = oi_df[oi_df["ms"] <= entry_ms]
    after = oi_df[oi_df["ms"] > entry_ms]
    if before.empty or after.empty:
        return {}
    base_oi = float(before.iloc[-1]["oi"])
    if base_oi <= 0:
        return {}
    out = {}
    for k in (1, 2, 3, 6):
        if len(after) < k:
            out[f"d_oi_{k}"] = None
            continue
        out[f"d_oi_{k}"] = (float(after.iloc[k - 1]["oi"]) - base_oi) / base_oi * 100.0
    return out


def main() -> None:
    t = pd.read_csv(TRADES_CSV)
    t = t[t["status"] == "CLOSED"].copy()
    print(f"成交 {len(t)} 笔")

    rows = []
    cache: dict[str, pd.DataFrame] = {}
    for r in t.itertuples():
        if r.symbol not in cache:
            cache[r.symbol] = load_oi_series(r.symbol)
        rows.append(oi_changes_after(int(r.entry_time), cache[r.symbol]))
    oi = pd.DataFrame(rows, index=t.index)
    m = pd.concat([t, oi], axis=1)
    m.to_csv("reports/oi_entry_based_analysis.csv", index=False)

    print("\n=== 按成交后 OI 方向分组 (d_oi_2: 成交后第2根5m) ===")
    m["up2"] = m["d_oi_2"] > 0
    for g, d in m.groupby("up2", group_keys=False):
        print(f"  OI上升: n={len(d)} 胜率={d.winner.mean():.1%} 净={d.net_pnl.sum():.1f}U "
              f"单均={d.net_pnl.mean():.1f}U 大亏={(d.net_pnl<-100).sum()}")

    print("\n=== 按 exit_reason 分组的 OI 变化 ===")
    for reason, d in m.groupby("exit_reason"):
        print(f"  {reason}: n={len(d)} d_oi_1={d.d_oi_1.median():+.2f}% "
              f"d_oi_2={d.d_oi_2.median():+.2f}% d_oi_3={d.d_oi_3.median():+.2f}%")

    print("\n=== 亏损单的 OI 特征（能否被 OI 过滤） ===")
    losses = m[m.net_pnl < 0]
    wins = m[m.net_pnl > 0]
    for k in (1, 2, 3):
        lk = losses[f"d_oi_{k}"].dropna()
        wk = wins[f"d_oi_{k}"].dropna()
        print(f"  +{k}根5m: 亏损中位 {lk.median():+.2f}% 上升率 {(lk>0).mean():.0%} "
              f"vs 盈利中位 {wk.median():+.2f}% 上升率 {(wk>0).mean():.0%}")

    print("\n=== 如果规则为'成交后第2根5m OI未降则止损'，被标记单的表现 ===")
    flagged = m[m.d_oi_2 > 0]
    kept = m[m.d_oi_2 <= 0]
    print(f"  OI未降(标记): n={len(flagged)} 其中盈利 {flagged.winner.sum()} 亏损 {len(flagged)-flagged.winner.sum()}")
    print(f"    被标记单净合计 {flagged.net_pnl.sum():.1f}U (若止损则这笔钱被牺牲/或避免)")
    print(f"  OI已降(保留): n={len(kept)} 胜率 {kept.winner.mean():.1%} 净 {kept.net_pnl.sum():.1f}U")
    print(f"  被标记单中真正亏损的: {flagged.net_pnl[flagged.net_pnl<0].sum():.1f}U "
          f"({(flagged.net_pnl<0).sum()}笔)，被误杀盈利 {(flagged.net_pnl>0).sum()}笔 {flagged.net_pnl[flagged.net_pnl>0].sum():.1f}U")


if __name__ == "__main__":
    main()