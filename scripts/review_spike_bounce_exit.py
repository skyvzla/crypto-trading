"""逐笔复盘 Spike V2 基线 70 笔：盈利单下跌见底后反弹才走的情况占比。

只读 DuckDB 历史归档，不回写、不联网。研究口径：
- 持仓期间 = [entry_time, exit_time]
- 对 SHORT，持仓最低价（1m low 最小值）= 最大盈利点
- "反弹后才走"：exit_price > 持仓最低价（价格从低点反弹后才退出）
- 时间限制判定：最低点出现时刻 elapsed 是否 < strict_age(15min)，
  以及最低点时刻 decay_agreement 是否达到"放宽后 required=1"水平。
"""

from __future__ import annotations

import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from trading_platform.strategies.spike.exit_features import (
    CandidateFeatureConfig,
    momentum_indicators,
)

REPORT = Path("reports/spike-v2-grouped-exit-time-full/all_trades.csv")
DUCKDB = Path("data/market/candles/candles.duckdb")
ARCHIVE_ROOT = Path("data/market/candles")
ARCHIVE_INDEX = Path("data/market/candles/archive_index.parquet")
STRICT_AGE_MS = 900_000  # 15min 基线
WARMUP_MINUTES = 90  # 指标预热（volatility 30 + slope 15 + macd 26）

QUERY = """
    SELECT epoch_ms(open_time) AS open_ms, open, high, low, close, volume
    FROM read_parquet(?, union_by_name=true)
    WHERE symbol = ? AND timeframe = '1m'
      AND epoch_ms(open_time) >= ? AND epoch_ms(open_time) < ?
    ORDER BY open_time
"""


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
    config = CandidateFeatureConfig()

    rows = []
    for _, t in baseline.iterrows():
        symbol = t["symbol"]
        entry_ms = int(t["entry_time"])
        exit_ms = int(t["exit_time"])
        if exit_ms <= entry_ms:
            exit_ms = entry_ms + 60_000
        klines = load_klines(
            con, symbol, entry_ms - WARMUP_MINUTES * 60_000, exit_ms + 60_000
        )
        if klines.empty:
            rows.append(
                {**t.to_dict(), "kline_ok": False, "note": "no klines"}
            )
            continue
        klines = klines[klines["open_ms"] >= entry_ms - WARMUP_MINUTES * 60_000]
        holding = klines[
            (klines["open_ms"] >= entry_ms) & (klines["open_ms"] < exit_ms)
        ]
        rows.append(analyze_trade(t, klines, holding, config))
    con.close()

    df = pd.DataFrame(rows)
    df.to_csv("reports/spike-v2-bounce-exit-review.csv", index=False)
    summarize(df)


def analyze_trade(
    t: pd.Series, klines: pd.DataFrame, holding: pd.DataFrame, config: CandidateFeatureConfig
) -> dict:
    base = t.to_dict()
    if holding.empty:
        return {**base, "kline_ok": False, "note": "no holding klines"}

    min_low = float(holding["low"].min())
    min_low_ms = int(holding.loc[holding["low"].idxmin(), "open_ms"])
    exit_price = float(t["exit_price"])
    bounce_pct = (exit_price / min_low - 1) * 100 if min_low > 0 else np.nan

    mom = momentum_indicators(klines.rename(columns={"open_ms": "available_ms"}), config)
    mom = mom[mom["available_ms"] >= min_low_ms - 0]  # 最低点可见的已完成 K 线
    latest = mom.iloc[-1]
    decay_at_low = (
        None
        if pd.isna(latest.get("decay_probe_agreement"))
        else int(latest["decay_probe_agreement"])
    )
    elapsed_at_low = min_low_ms - int(t["entry_time"])
    elapsed_exit = int(t["exit_time"]) - int(t["entry_time"])

    required_at_low = (
        3
        if elapsed_at_low < 5 * 60_000
        else 2
        if elapsed_at_low < STRICT_AGE_MS
        else 1
    )
    exit_reason = str(t["exit_reason"])
    time_limited = elapsed_at_low < STRICT_AGE_MS
    decay_at_low_val = decay_at_low if decay_at_low is not None else 0
    time_limit_cause = bool(
        time_limited and decay_at_low_val >= 1 and decay_at_low_val < required_at_low
    )
    no_signal_at_low = bool(decay_at_low is None or decay_at_low < 1)

    return {
        **base,
        "kline_ok": True,
        "min_low": min_low,
        "min_low_ms": min_low_ms,
        "bounce_pct": bounce_pct,
        "elapsed_at_low_ms": elapsed_at_low,
        "elapsed_exit_ms": elapsed_exit,
        "decay_agreement_at_low": decay_at_low,
        "required_at_low": required_at_low,
        "momentum_satisfied_at_low": (
            decay_at_low is not None and decay_at_low >= required_at_low
        ),
        "momentum_satisfied_if_relaxed": (
            decay_at_low is not None and decay_at_low >= 1
        ),
        "time_limited_low": time_limited,
        "time_limit_cause": time_limit_cause,
        "no_signal_at_low": no_signal_at_low,
        "bounced_exit": bool(exit_price > min_low),
    }


def summarize(df: pd.DataFrame) -> None:
    ok = df[df["kline_ok"]]
    print(f"\nK 线可用: {len(ok)}/{len(df)}")

    bounced = ok[ok["bounced_exit"]]
    winners = ok[ok["winner"]]
    bounced_winners = ok[ok["winner"] & ok["bounced_exit"]]

    print(f"\n=== 1. 反弹后才走（exit_price > 持仓最低价）===")
    print(f"全部 {len(ok)} 笔中反弹后才走: {len(bounced)} ({len(bounced)/len(ok):.1%})")
    print(f"盈利单 {len(winners)} 笔中反弹后才走: {len(bounced_winners)} ({len(bounced_winners)/len(winners):.1%})")

    print(f"\n=== 2. 反弹幅度分布（全部单, exit_price/持仓最低价-1）===")
    if not bounced.empty:
        print(f"反弹幅度 P25/P50/P75: "
              f"{bounced['bounce_pct'].quantile(0.25):.2f}% / "
              f"{bounced['bounce_pct'].quantile(0.5):.2f}% / "
              f"{bounced['bounce_pct'].quantile(0.75):.2f}%")
        print(f"反弹>1%: {(bounced['bounce_pct']>1).sum()}, "
              f">2%: {(bounced['bounce_pct']>2).sum()}, "
              f">5%: {(bounced['bounce_pct']>5).sum()}")

    print(f"\n=== 3. 最低点出现时刻 vs 15min 时间窗（反弹后才走的单）===")
    tl = bounced["time_limited_low"].sum()
    print(f"最低点在 <15min 内出现（时间限制拦截）: {tl}/{len(bounced)} "
          f"({tl/len(bounced):.1%})")
    if not bounced.empty:
        print("最低点 elapsed 分布: "
              f"<5min: {(bounced['elapsed_at_low_ms']<300000).sum()}, "
              f"5-15min: {((bounced['elapsed_at_low_ms']>=300000)&(bounced['elapsed_at_low_ms']<900000)).sum()}, "
              f">=15min: {(bounced['elapsed_at_low_ms']>=900000).sum()}")

    print(f"\n=== 4. 最低点时刻动量条件 ===")
    if not bounced.empty:
        sat = bounced["momentum_satisfied_at_low"].sum()
        relaxed = bounced["momentum_satisfied_if_relaxed"].sum()
        print(f"最低点时刻 required({3}) 满足: {sat}/{len(bounced)}")
        print(f"最低点时刻若放宽 required=1 可满足: {relaxed}/{len(bounced)}")
        print(f"最低点时刻时间已到 15min 且动量满足: "
              f"{((~bounced['time_limited_low']) & bounced['momentum_satisfied_at_low']).sum()}/{(~bounced['time_limited_low']).sum()}")

    print(f"\n=== 4b. 时间限制 vs 无信号（反弹后才走的单）===")
    if not bounced.empty:
        tc = bounced["time_limit_cause"].sum()
        ns = bounced["no_signal_at_low"].sum()
        print(f"时间限制直接原因（最低点在<15min 且 1<=decay<required）: {tc}/{len(bounced)} ({tc/len(bounced):.1%})")
        print(f"低点时刻无衰减信号（decay<1，非时间限制）: {ns}/{len(bounced)} ({ns/len(bounced):.1%})")
        print(f"低点时刻动量已达标但未退（执行/阻塞类）: "
              f"{len(bounced)-tc-ns}/{len(bounced)}")

    print(f"\n=== 4c. 明显反弹（bounce>2%）的因果分类 ===")
    big2 = ok[ok["bounce_pct"] > 2]
    if not big2.empty:
        tc = big2["time_limit_cause"].sum()
        ns = big2["no_signal_at_low"].sum()
        print(f">2% 反弹: {len(big2)} 笔; 时间限制原因 {tc} ({tc/len(big2):.1%}), "
              f"无信号 {ns} ({ns/len(big2):.1%})")

    print(f"\n=== 5. 退出原因分布（反弹后才走的单）===")
    if not bounced.empty:
        print(bounced["exit_reason"].value_counts().to_string())

    print(f"\n=== 6. 反弹后才走 > 1% 的明细 ===")
    big = ok[ok["bounce_pct"] > 1]
    if not big.empty:
        print(big[
            ["symbol", "entry_time", "exit_time", "exit_reason", "net_pnl", "winner",
             "min_low", "exit_price", "bounce_pct", "elapsed_at_low_ms", "time_limited_low"]
        ].to_string(index=False))


if __name__ == "__main__":
    main()