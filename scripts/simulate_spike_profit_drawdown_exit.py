"""模拟：盈利达阈值后弱化时间限制，按"动能(decay)+峰值回撤"退出。

对照基线 70 笔。只读 DuckDB。

三种退出规则（盈利峰值达到 gain 阈值后生效）：
- R1 纯回撤：价格从峰值回撤 >= dd% 即退（不看动能）
- R2 弱化时间+回撤：decay_agreement>=1 且 回撤>=dd% 才退
- R3 弱化时间（纯动量）：decay_agreement>=1 即退（动量分档降到 1，不再等 15min）

用法: python3 scripts/simulate_spike_profit_drawdown_exit.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import duckdb
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from trading_platform.strategies.spike.exit_features import (
    CandidateFeatureConfig,
    momentum_indicators,
)

REPORT = Path("reports/spike-v2-grouped-exit-time-full/all_trades.csv")
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

GAIN_THRESHOLDS = [0.05, 0.10, 0.15, 0.20]
DRAWDOWN_THRESHOLDS = [0.005, 0.01, 0.02, 0.03, 0.05]


def load_klines(con, symbol: str, start_ms: int, end_ms: int) -> pd.DataFrame:
    idx = pd.read_parquet(ARCHIVE_INDEX)
    selected = idx[
        (idx["symbol"] == symbol)
        & (idx["timeframe"] == "1m")
        & (idx["first_open_ms"] < end_ms)
        & (idx["last_close_ms"] >= start_ms)
    ]
    if selected.empty:
        return pd.DataFrame(
            columns=["open_ms", "open", "high", "low", "close", "volume"]
        )
    files = [str(ARCHIVE_ROOT / p) for p in selected["relative_path"]]
    rows = con.execute(QUERY, [files, symbol, start_ms, end_ms]).fetchall()
    return pd.DataFrame(
        rows, columns=["open_ms", "open", "high", "low", "close", "volume"]
    )


def simulate_trade(
    entry_ms: int,
    entry_price: float,
    entry_qty: float,
    commission: float,
    frame: pd.DataFrame,
    config: CandidateFeatureConfig,
) -> dict[str, float]:
    results: dict[str, float] = {}
    frame = frame.reset_index(drop=True)
    mom = momentum_indicators(frame.rename(columns={"open_ms": "available_ms"}), config)
    peak_price = entry_price
    for idx, row in mom.iterrows():
        if int(row["available_ms"]) < entry_ms:
            continue
        close = float(row["close"])
        peak_price = min(peak_price, close)
        gain = (entry_price - peak_price) / entry_price
        drawdown = (close - peak_price) / peak_price if peak_price > 0 else 0.0
        decay = row.get("decay_probe_agreement")
        decay = None if pd.isna(decay) else int(decay)
        for gt in GAIN_THRESHOLDS:
            if gain < gt:
                continue
            for dd in DRAWDOWN_THRESHOLDS:
                key_r1 = f"r1_g{gt:.2f}_dd{dd:.3f}"
                if key_r1 not in results and drawdown >= dd:
                    results[key_r1] = (entry_price - close) * entry_qty - commission
                key_r2 = f"r2_g{gt:.2f}_dd{dd:.3f}"
                if key_r2 not in results and decay is not None and decay >= 1 and drawdown >= dd:
                    results[key_r2] = (entry_price - close) * entry_qty - commission
            key_r3 = f"r3_g{gt:.2f}"
            if key_r3 not in results and decay is not None and decay >= 1:
                results[key_r3] = (entry_price - close) * entry_qty - commission
    return results


def main() -> None:
    trades = pd.read_csv(REPORT)
    baseline = trades[trades["parameters"].str.contains("900000")].copy()
    baseline = baseline[baseline["status"] == "CLOSED"].reset_index(drop=True)
    print(f"基线交易数: {len(baseline)}")

    con = duckdb.connect()
    config = CandidateFeatureConfig()
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
        klines = klines[klines["open_ms"] >= entry_ms - WARMUP_MINUTES * 60_000]
        sim = simulate_trade(
            entry_ms,
            float(t["entry_price"]),
            float(t["entry_quantity"]),
            float(t["commission"]),
            klines,
            config,
        )
        row = {
            "symbol": t["symbol"],
            "entry_time": entry_ms,
            "exit_time": exit_ms,
            "exit_reason": t["exit_reason"],
            "actual_pnl": float(t["net_pnl"]),
        }
        row.update(sim)
        rows.append(row)
    con.close()

    df = pd.DataFrame(rows)
    df.to_csv("reports/spike-v2-momentum-drawdown-exit-simulation.csv", index=False)

    actual_total = df["actual_pnl"].sum()
    print(f"\n实际总净收益: {actual_total:.1f}U (n={len(df)})")

    for gt in GAIN_THRESHOLDS:
        print(f"\n=== 盈利达 {gt:.0%} 后触发（未触发笔沿用实际收益）===")
        print(f"{'规则':<8}{'触发数':>6}  {'总收益U':>10}  {'Δvs实际':>9}")
        for dd in DRAWDOWN_THRESHOLDS:
            for rule in ("r1", "r2"):
                col = f"{rule}_g{gt:.2f}_dd{dd:.3f}"
                filled = df[col].fillna(df["actual_pnl"])
                trig = df[col].notna().sum()
                total = filled.sum()
                print(f"{rule} dd={dd:.1%}{'':<6}{trig:>6}  {total:>10.1f}  {total-actual_total:>+9.1f}")
        col = f"r3_g{gt:.2f}"
        filled = df[col].fillna(df["actual_pnl"])
        trig = df[col].notna().sum()
        total = filled.sum()
        print(f"{'r3 纯动量':<8}{trig:>6}  {total:>10.1f}  {total-actual_total:>+9.1f}")


if __name__ == "__main__":
    main()