"""模拟止损规则：OI 不降 + 浮亏达阈值 → 止损。

基准 = 插针最高点(signal_time 对应 bar)：
- 上一个 OI 点: available_time <= signal_time 的最近 5m 点
- 下一个 OI 点: available_time >  signal_time 的最近 5m 点（插针确认后的第一个 OI）
- d_oi = (下一个 - 上一个)/上一个 × 100
止损条件: d_oi > oi_floor 且确认时点浮亏 >= loss% 则按确认时点价格平仓。
"""
from __future__ import annotations

from pathlib import Path

import duckdb
import pandas as pd

TRADES_CSV = Path("reports/spike-v2.2-db-full-reject-only/all_trades.csv")
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


def simulate(oi_floor: float, loss_pct: float) -> dict:
    t = pd.read_csv(TRADES_CSV)
    t = t[t["status"] == "CLOSED"].copy()
    t["signal_ms"] = t["signal_time"].astype("int64")

    sim_pnls = []
    stopped = 0
    details = []
    for r in t.itertuples():
        oi_df = load_oi(r.symbol)
        px_df = load_px(r.symbol)
        if oi_df.empty or px_df.empty:
            sim_pnls.append((float(r.net_pnl), False, r.signal_ms, None))
            continue
        signal_ms = int(r.signal_ms)
        prev_oi = oi_df[oi_df["ms"] <= signal_ms]
        next_oi = oi_df[oi_df["ms"] > signal_ms]
        if prev_oi.empty or next_oi.empty:
            sim_pnls.append((float(r.net_pnl), False, r.signal_ms, None))
            continue
        prev_ms = int(prev_oi.iloc[-1]["ms"])
        prev_val = float(prev_oi.iloc[-1]["oi"])
        confirm_ms = int(next_oi.iloc[0]["ms"])
        confirm_val = float(next_oi.iloc[0]["oi"])
        if prev_val <= 0:
            sim_pnls.append((float(r.net_pnl), False, r.signal_ms, None))
            continue
        d_oi = (confirm_val - prev_val) / prev_val * 100.0

        # 确认时点价格: 确认 OI 点之前最近的 1m close
        px_at = px_df[px_df["ms"] <= confirm_ms]
        if px_at.empty:
            sim_pnls.append((float(r.net_pnl), False, r.signal_ms, d_oi))
            continue
        mark = float(px_at.iloc[-1]["close"])
        entry_price = float(r.entry_price)
        unreal = (entry_price - mark) / entry_price * 100.0

        if d_oi > oi_floor and unreal <= -loss_pct:
            gross = unreal / 100.0 * float(r.entry_notional)
            commission = abs(float(r.commission)) * (mark / float(r.exit_price))
            stop_pnl = gross - commission
            sim_pnls.append((stop_pnl, True, r.signal_ms, d_oi))
            stopped += 1
            details.append({
                "symbol": r.symbol, "signal_time": r.signal_time,
                "d_oi": round(d_oi, 2), "unreal_confirm": round(unreal, 2),
                "net_pnl": round(float(r.net_pnl), 1), "stop_pnl": round(stop_pnl, 1),
                "exit_reason": r.exit_reason,
            })
        else:
            sim_pnls.append((float(r.net_pnl), False, r.signal_ms, d_oi))

    total_orig = float(t["net_pnl"].sum())
    total_sim = sum(p for p, _, _, _ in sim_pnls)
    return {
        "sim": total_sim, "diff": total_sim - total_orig,
        "stopped": stopped, "orig": total_orig, "details": details,
    }


def main() -> None:
    print("=== 规则: 插针确认后第一个OI点 d_oi>floor 且该时点浮亏>=loss% 则止损 ===")
    print(f"{'OI>':>5} {'亏损>=':>6} {'止损数':>5} {'模拟总U':>9} {'vs原U':>9}")
    best = None
    for oi_floor in (0.0, 1.0, 3.0, 5.0):
        for loss_pct in (1.0, 2.0, 3.0, 5.0):
            res = simulate(oi_floor, loss_pct)
            print(f"{oi_floor:>5.1f} {loss_pct:>6.1f} {res['stopped']:>5} "
                  f"{res['sim']:>9.1f} {res['diff']:>+9.1f}")
            if best is None or res["diff"] > best["diff"]:
                best = {**res, "oi_floor": oi_floor, "loss_pct": loss_pct}
    print(f"\n最优: OI>{best['oi_floor']}% 亏损>={best['loss_pct']}% -> {best['diff']:+.1f}U")
    df = pd.DataFrame(best["details"])
    if not df.empty:
        df.to_csv("reports/oi_stop_details.csv", index=False)
        print(f"止损明细 {len(df)} 笔 -> reports/oi_stop_details.csv")
        print(df[["symbol", "d_oi", "unreal_confirm", "net_pnl", "stop_pnl", "exit_reason"]].head(20).to_string(index=False))


if __name__ == "__main__":
    main()