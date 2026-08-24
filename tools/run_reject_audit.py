"""Reject 审计：把 W3/W3-B 研究的过滤器套到 217 笔真实 spike 做空交易上。

过滤器（全部因果可得，signal_time 时点已确认）:
  F1 前兆警戒: signal 前 20 分钟内出现过 atr_mult>5 & wick>4%（W4 冻结规则）
  F2 平静首根: 触发 spike 满足 first_spike_60(前60根无spike>=10%) 且 pre_bbw_mult<0.8
统计各过滤器拒绝/放行组的 net_return、win rate、总 PnL。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

ROOT = Path("data/market/candles")
TRADES = "reports/spike-v2-grouped-exit-time-grid/all_trades.csv"
WARN_ATR = 5.0
WARN_WICK = 4.0
WARN_WINDOW_MS = 20 * 60_000
SPIKE_LEVEL = 0.10
FEE_SLIP = 0.0


def load_symbol_1m(symbol: str) -> pd.DataFrame | None:
    path = ROOT / symbol / "1m" / "**" / "*.parquet"
    if not list(ROOT.glob(f"{symbol}/1m/2026")) and not list(ROOT.glob(f"{symbol}/1m/2025")):
        return None
    con = duckdb.connect()
    try:
        df = con.execute(
            """SELECT epoch_ms(open_time)::BIGINT AS open_ms, open, high, low, close, volume
               FROM read_parquet(?, union_by_name=true) ORDER BY open_time""",
            [str(path)],
        ).fetchdf()
    except Exception:
        return None
    finally:
        con.close()
    return df if len(df) > 300 else None


def compute_indicators(bars: pd.DataFrame) -> pd.DataFrame:
    h = bars["high"].to_numpy(float)
    l = bars["low"].to_numpy(float)
    c = bars["close"].to_numpy(float)
    v = pd.to_numeric(bars["volume"], errors="coerce").to_numpy(float)
    prev_c = np.roll(c, 1)
    prev_c[0] = c[0]
    tr = np.maximum(h - l, np.maximum(np.abs(h - prev_c), np.abs(l - prev_c)))
    atr = pd.Series(tr).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean().to_numpy()
    atr_ratio = atr / np.maximum(c, 1e-12)
    out = bars.copy()
    out["atr_mult"] = atr_ratio / pd.Series(atr_ratio).rolling(30, min_periods=30).median().to_numpy()
    tp = (h + l + c) / 3.0
    sma = pd.Series(tp).rolling(20, min_periods=20).mean()
    std = pd.Series(tp).rolling(20, min_periods=20).std(ddof=1)
    bbw = (4 * std / sma).to_numpy()
    out["pre_bbw_mult"] = bbw / pd.Series(bbw).shift(1).rolling(240, min_periods=240).median().to_numpy()
    out["wick"] = (h / np.maximum(c, 1e-12) - 1.0) * 100.0
    out["is_spike10"] = h >= prev_c * (1.0 + SPIKE_LEVEL)
    vol_base = pd.Series(v).rolling(60, min_periods=60).median().shift(1).to_numpy()
    out["vol_mult"] = v / np.where(vol_base > 0, vol_base, np.nan)
    # warn_active: 当前 bar 收盘时，过去 20 分钟内（含当前）是否出现过前兆命中
    warn_hit = (out["atr_mult"] > WARN_ATR) & (out["wick"] > WARN_WICK)
    hit_idx = np.where(warn_hit.to_numpy())[0]
    warn_active = np.zeros(len(out), dtype=bool)
    times = out["open_ms"].to_numpy(np.int64)
    for i in hit_idx:
        lo = np.searchsorted(times, times[i] - WARN_WINDOW_MS, side="left")
        warn_active[lo : i + 1] = True
    out["warn_active"] = warn_active
    return out


def main() -> int:
    started = time.monotonic()
    trades = pd.read_csv(TRADES)
    trades = trades[trades["status"] == "CLOSED"].copy()
    audit_rows: list[dict[str, object]] = []
    for symbol, group in trades.groupby("symbol"):
        bars = load_symbol_1m(symbol)
        if bars is None:
            print(f"{symbol}: 无 1m 归档，跳过 {len(group)} 笔", flush=True)
            continue
        ind = compute_indicators(bars)
        times = ind["open_ms"].to_numpy(np.int64)
        spikes = np.where(ind["is_spike10"].to_numpy())[0]
        for row in group.itertuples():
            signal_ms = int(row.signal_time)
            pos = int(np.searchsorted(times, signal_ms, side="right")) - 1
            if pos < 250:
                continue
            f1 = bool(ind.iloc[pos]["warn_active"])
            prior_spikes = spikes[(spikes < pos - 1) & (spikes >= pos - 1 - 60)]
            first_spike = len(prior_spikes) == 0
            pre_bbw = ind.iloc[pos - 1]["pre_bbw_mult"] if pos >= 1 else np.nan
            f2 = bool(first_spike and np.isfinite(pre_bbw) and pre_bbw < 0.8)
            audit_rows.append({
                "trade_id": row.trade_id,
                "symbol": symbol,
                "signal_time": signal_ms,
                "net_return": float(row.net_return),
                "net_pnl": float(row.net_pnl),
                "winner": bool(row.winner),
                "f1_warn_window": f1,
                "f2_calm_first": f2,
                "first_spike_60": first_spike,
                "pre_bbw_mult": float(pre_bbw) if np.isfinite(pre_bbw) else np.nan,
                "vol_mult": float(ind.iloc[pos]["vol_mult"]),
                "in_sample_hint": signal_ms < int(pd.Timestamp("2026-05-01", tz="UTC").timestamp() * 1000),
            })
    audit = pd.DataFrame(audit_rows)
    audit.to_csv("/tmp/opencode/reject_audit.csv", index=False)

    lines = [
        "# Reject 审计：研究过滤器 × 217 笔真实 spike 做空交易", "",
        f"- 交易来源: reports/spike-v2-grouped-exit-time-grid/all_trades.csv（CLOSED）",
        f"- 审计样本: {len(audit)} 笔（有 1m 归档且历史足够）",
        "- F1 前兆警戒: signal 前 20 分钟内出现 `atr>5x & wick>4%`（W4 冻结规则）",
        "- F2 平静首根: 前 60 根无 spike≥10% 且 pre_bbw<0.8x（W3-B 分层）",
        "",
        "## 过滤器与交易结果交叉",
        "",
    ]

    def block(name: str, sub: pd.DataFrame) -> str:
        if sub.empty:
            return f"| {name} | 0 | - | - | - |"
        return (
            f"| {name} | {len(sub)} | {sub['net_return'].mean():+.4f} "
            f"| {sub['winner'].mean():.1%} | {sub['net_pnl'].sum():+.2f} |"
        )

    lines += [
        "| 分组 | n | net_return 均值 | win | 总 PnL |",
        "|---|---:|---:|---:|---:|",
        block("全部交易", audit),
        block("F1 警戒窗口内(放行)", audit[audit["f1_warn_window"]]),
        block("F1 窗口外(拒绝)", audit[~audit["f1_warn_window"]]),
        block("F2 平静首根(放行)", audit[audit["f2_calm_first"]]),
        block("F2 非平静首根(拒绝)", audit[~audit["f2_calm_first"]]),
        block("F1&F2 同时放行", audit[audit["f1_warn_window"] & audit["f2_calm_first"]]),
        "",
        "## 样本外视角（signal >= 2026-05-01）",
        "",
    ]
    oos = audit[~audit["in_sample_hint"]]
    lines += [
        "| 分组 | n | net_return 均值 | win | 总 PnL |",
        "|---|---:|---:|---:|---:|",
        block("OOS 全部", oos),
        block("OOS F1 放行", oos[oos["f1_warn_window"]]),
        block("OOS F1 拒绝", oos[~oos["f1_warn_window"]]),
        block("OOS F2 放行", oos[oos["f2_calm_first"]]),
        block("OOS F2 拒绝", oos[~oos["f2_calm_first"]]),
        "",
    ]
    out = Path("docs/research/REJECT_AUDIT_W5.md")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"审计完成 {len(audit)} 笔，耗时 {time.monotonic()-started:.0f}s -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
