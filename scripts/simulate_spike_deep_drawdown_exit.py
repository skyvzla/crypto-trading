"""扩展模拟：大阈值峰值回撤止盈（纯价格，不看动能/时间）。

候选规则：浮盈峰值已建立（从 entry 回撤 >= gt）后，价格从峰值
（做空=最低 close）反弹 >= dd 即退出。gt ∈ {0.05, 0.10, 0.20}，
dd ∈ {0.05, 0.08, 0.10, 0.12, 0.15, 0.20}。对比基线 70 笔；
未触发沿用基线实际 pnl。关键：浮盈前置条件避免"价格从 entry 上涨
即触发"的伪回撤。

只读 DuckDB 历史归档，不回写、不联网。
"""

from __future__ import annotations

import sys
from pathlib import Path

import duckdb
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

REPORT = Path("reports/spike-v2-grouped-exit-time-full/all_trades.csv")
DUCKDB = Path("data/market/candles/candles.duckdb")
ARCHIVE_ROOT = Path("data/market/candles")
ARCHIVE_INDEX = Path("data/market/candles/archive_index.parquet")
WARMUP_MINUTES = 90

QUERY = """
    SELECT epoch_ms(open_time) AS open_ms, open, high, low, close, volume
    FROM read_parquet(?, union_by_name=true)
    WHERE symbol = ? AND timeframe = '1m'
      AND epoch_ms(open_time) >= ? AND epoch_ms(open_time) < ?
    ORDER BY open_time
"""

GAIN_THRESHOLDS = [0.20, 0.25]
DRAWDOWN_THRESHOLDS = [0.06, 0.08, 0.10]


def load_klines(con, symbol: str, start_ms: int, end_ms: int) -> pd.DataFrame:
    idx = pd.read_parquet(ARCHIVE_INDEX)
    selected = idx[
        (idx["symbol"] == symbol)
        & (idx["timeframe"] == "1m")
        & (idx["first_open_ms"] < end_ms)
        & (idx["last_close_ms"] >= start_ms)
    ]
    if selected.empty:
        return pd.DataFrame(columns=["open_ms", "open", "high", "low", "close", "volume"])
    files = [str(ARCHIVE_ROOT / p) for p in selected["relative_path"]]
    rows = con.execute(QUERY, [files, symbol, start_ms, end_ms]).fetchall()
    return pd.DataFrame(
        rows, columns=["open_ms", "open", "high", "low", "close", "volume"]
    )


def main() -> None:
    trades = pd.read_csv(REPORT)
    baseline = trades[trades["parameters"].str.contains("900000")].copy()
    baseline = baseline[baseline["status"] == "CLOSED"].reset_index(drop=True)
    print(f"基线交易数: {len(baseline)}")

    con = duckdb.connect(str(DUCKDB), read_only=True)
    rows = []
    for _, t in baseline.iterrows():
        entry_ms, exit_ms = int(t["entry_time"]), int(t["exit_time"])
        if exit_ms <= entry_ms:
            exit_ms = entry_ms + 60_000
        klines = load_klines(
            con, t["symbol"], entry_ms - WARMUP_MINUTES * 60_000, exit_ms
        )
        if klines.empty:
            continue
        holding = klines[
            (klines["open_ms"] >= entry_ms) & (klines["open_ms"] < exit_ms)
        ]
        row = {
            "symbol": t["symbol"],
            "entry_time": entry_ms,
            "exit_time": exit_ms,
            "exit_reason": t["exit_reason"],
            "actual_pnl": float(t["net_pnl"]),
        }
        if holding.empty:
            rows.append(row)
            continue
        entry_price = float(t["entry_price"])
        entry_qty = float(t["entry_quantity"])
        commission = float(t["commission"])
        peak_price = entry_price
        triggered: dict[str, float] = {}
        for _, k in holding.iterrows():
            close = float(k["close"])
            peak_price = min(peak_price, close)
            gain_peak = (entry_price - peak_price) / entry_price
            drawdown = (close - peak_price) / peak_price if peak_price > 0 else 0.0
            for gt in GAIN_THRESHOLDS:
                if gain_peak < gt:
                    continue
                for dd in DRAWDOWN_THRESHOLDS:
                    key = f"g{gt:.2f}_dd{dd:.2f}"
                    if key not in triggered and drawdown >= dd:
                        triggered[key] = (entry_price - close) * entry_qty - commission
        for gt in GAIN_THRESHOLDS:
            for dd in DRAWDOWN_THRESHOLDS:
                key = f"g{gt:.2f}_dd{dd:.2f}"
                row[key] = triggered.get(key, float("nan"))
        rows.append(row)
    con.close()

    df = pd.DataFrame(rows)
    df.to_csv("reports/spike-v2-deep-drawdown-simulation.csv", index=False)

    actual_total = df["actual_pnl"].sum()
    print(f"\n实际总净收益: {actual_total:.1f}U (n={len(df)})")
    for gt in GAIN_THRESHOLDS:
        for dd in DRAWDOWN_THRESHOLDS:
            key = f"g{gt:.2f}_dd{dd:.2f}"
            triggered = df[key].notna()
            total = df.loc[triggered, key].sum() + df.loc[~triggered, "actual_pnl"].sum()
            n_triggered = int(triggered.sum())
            print(
                f"浮盈>={gt:.0%} 回撤>={dd:.0%}: 触发 {n_triggered:2d} 笔 "
                f"{total:8.1f}U (Δ{total-actual_total:+7.1f})"
            )


if __name__ == "__main__":
    main()