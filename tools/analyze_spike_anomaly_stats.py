"""异动事件大样本统计分析。

输入:
  reports/spike_anomaly_metrics.csv        (1m/5m/15m 指标 + 异动后走势)
  reports/spike_anomaly_metrics_1s.csv     (1s 周期指标)
  reports/daily_amplitude_over_50pct.csv   (事件清单: 方向/时长/幅度)

研究目标（对应策略诉求）:
  1. 信号点: 异动事件本身的时间/幅度/方向分布规律
  2. 过滤: 哪些指标能显著提升"异动后回落"概率（做空视角，upward）
  3. 止损止盈: 异动后最大不利的时间分布、回落的时间结构、反转规律

输出: docs/research/SPIKE_ANOMALY_STATS.md
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

pd.set_option("display.width", 220)
pd.set_option("display.max_columns", 60)
pd.set_option("display.float_format", lambda x: f"{x:8.1f}")


def build_table() -> pd.DataFrame:
    m = pd.read_csv("reports/spike_anomaly_metrics.csv")
    s = pd.read_csv("reports/spike_anomaly_metrics_1s.csv")
    s = s[["symbol", "event_day", "direction", "osc_end_utc"] + [c for c in s.columns if c.startswith(("rise_", "vol_mult_", "pulse_1s", "spike_seconds", "fwd_max_1s"))]]
    df = m.merge(s, on=["symbol", "event_day", "direction"], how="left")
    return df


def pct_row(label: str, s: pd.Series, fmt: str = "{:.1f}") -> str:
    q = s.quantile([0.05, 0.25, 0.5, 0.75, 0.95]).round(1)
    return f"| {label} | {q.iloc[0]:.1f} | {q.iloc[1]:.1f} | {q.iloc[2]:.1f} | {q.iloc[3]:.1f} | {q.iloc[4]:.1f} |"


def split_stats(df: pd.DataFrame, col: str, target: str, th_pairs: list[tuple[float, float]]) -> pd.DataFrame:
    """按 col 分位数切两层，统计 target 各阈值命中率。"""
    rows = []
    s = pd.to_numeric(df[col], errors="coerce")
    sub = df[s.notna()].copy()
    for lo_q, hi_q in th_pairs:
        lo = s.quantile(lo_q)
        hi = s.quantile(hi_q)
        a = sub[s <= lo]
        b = sub[s > hi]
        rows.append(
            {
                "col": col,
                "layer": f"<=P{int(lo_q*100)}",
                "n": len(a),
                "hit": (a[target] < th_pairs[0][0]).mean() * 100 if target.startswith("ret_after") else np.nan,
                "target_med": a[target].median(),
                "target_mean": a[target].mean(),
            }
        )
        rows.append(
            {
                "col": col,
                "layer": f">P{int(hi_q*100)}",
                "n": len(b),
                "hit": (b[target] < th_pairs[0][0]).mean() * 100 if target.startswith("ret_after") else np.nan,
                "target_med": b[target].median(),
                "target_mean": b[target].mean(),
            }
        )
    return pd.DataFrame(rows)


def main() -> None:
    df = build_table()
    up = df[df.direction == "upward"].copy()
    dn = df[df.direction == "downward"].copy()
    print(f"总事件 {len(df)}: upward {len(up)}, downward {len(dn)}")

    lines: list[str] = []
    lines.append("# 异动事件大样本统计（2025-08 ~ 2026-07）\n")
    lines.append(f"> 数据: {len(df)} 事件 / {df.symbol.nunique()} 币。upward {len(up)} 笔 / downward {len(dn)} 笔。\n")

    # ============ 1. 信号点特征 ============
    lines.append("## 1. 信号点特征（异动事件分布）\n")
    lines.append("### 1.1 时间/幅度/时长分布\n")
    lines.append("| 指标 | P5 | P25 | P50 | P75 | P95 |")
    lines.append("|---|---|---|---|---|---|")
    lines.append(pct_row("amplitude_pct (日振幅%)", df.amplitude_pct))
    lines.append(pct_row("osc_duration_min (振荡时长分)", df.osc_duration_min))
    lines.append(pct_row("osc_duration_hours (振荡时长时)", df.osc_duration_min / 60))
    lines.append(pct_row("box_slope_bps (前3d箱体斜率)", df.box_slope_bps, "{:.2f}"))
    lines.append(pct_row("vwap_dev_5m (VWAP偏离%)", df.vwap_dev_5m))
    lines.append(pct_row("atr_ratio_5m (ATR/价)", df.atr_ratio_5m, "{:.3f}"))
    lines.append(pct_row("rsi_5m", df.rsi_5m))
    lines.append(pct_row("roc_5m (5m ROC%)", df.roc_5m, "{:.2f}"))
    lines.append(pct_row("pulse_1m (最大单分钟脉冲%)", df.pulse_1m, "{:.2f}"))
    lines.append(pct_row("rise_5s (5s涨幅%)", df.rise_5s, "{:.2f}"))
    lines.append(pct_row("vol_mult_5s (5s量倍数)", df.vol_mult_5s, "{:.2f}"))
    lines.append(pct_row("pulse_1s_max (最大单秒脉冲%)", df.pulse_1s_max, "{:.2f}"))
    lines.append(pct_row("spike_seconds_ratio (30min成交秒占比)", df.spike_seconds_ratio, "{:.3f}"))
    lines.append("")

    # ============ 2. 过滤方式 ============
    lines.append("## 2. 过滤方式（upward 异动，做空视角）\n")
    sub = up[up.ret_after_15m.notna()].copy()
    lines.append(f"### 2.1 基准: 异动后回落概率\n")
    lines.append("| 窗口 | 回落<-5% | 回落<-10% | 中位 | 均值 |")
    lines.append("|---|---|---|---|---|")
    for c in ["ret_after_15m", "ret_after_30m", "ret_after_45m", "ret_after_1h", "ret_after_4h"]:
        s = sub[c]
        lines.append(f"| {c} | {(s < -5).mean()*100:.0f}% | {(s < -10).mean()*100:.0f}% | {s.median():.1f} | {s.mean():.1f} |")
    lines.append("")

    lines.append("### 2.2 最大不利（做空后短窗口冲高）\n")
    lines.append("| 窗口 | 冲>3% | 冲>5% | 冲>10% | 中位 |")
    lines.append("|---|---|---|---|---|")
    for c in ["fwd_max_15m", "fwd_max_30m", "fwd_max_1s_60s", "fwd_max_1s_15m", "fwd_max_1s_30m"]:
        s = pd.to_numeric(sub[c], errors="coerce")
        if s.notna().sum() < 100:
            continue
        lines.append(f"| {c} | {(s > 3).mean()*100:.0f}% | {(s > 5).mean()*100:.0f}% | {(s > 10).mean()*100:.0f}% | {s.median():.1f} |")
    lines.append("")

    lines.append("### 2.3 指标分层过滤（各指标 P50 分两层 vs 回落概率）\n")
    lines.append("| 指标 | 低层<-5%率 | 低层中位 | 高层<-5%率 | 高层中位 | 差 |")
    lines.append("|---|---|---|---|---|---|")
    for col in ["vwap_dev_5m", "atr_ratio_5m", "bb_width_5m", "ema_ratio_5m", "roc_5m",
                "rsi_5m", "pulse_1m", "accel_5m", "rise_5s", "vol_mult_5s", "pulse_1s_max",
                "vol_cv_1h", "osc_start_over_up", "osc_end_over_up"]:
        s = pd.to_numeric(sub[col], errors="coerce")
        if s.notna().sum() < 100:
            continue
        med = s.median()
        lo = sub[s <= med]
        hi = sub[s > med]
        t = "ret_after_15m"
        lines.append(
            f"| {col} | {((lo[t] < -5).mean()*100):.0f}% | {lo[t].median():.1f} | "
            f"{((hi[t] < -5).mean()*100):.0f}% | {hi[t].median():.1f} | "
            f"{((hi[t] < -5).mean()*100 - (lo[t] < -5).mean()*100):+.0f}pp |"
        )
    lines.append("")

    # ============ 3. 止损止盈规律 ============
    lines.append("## 3. 止损止盈规律（upward 异动）\n")
    lines.append("### 3.1 回落的时间结构: 回落<-5% 需要多久\n")
    lines.append("| 窗口 | 累计回落<-5% 概率 |")
    lines.append("|---|---|")
    for c in ["ret_after_15m", "ret_after_30m", "ret_after_45m", "ret_after_1h"]:
        lines.append(f"| {c} | {(sub[c] < -5).mean()*100:.0f}% |")
    lines.append("")

    lines.append("### 3.2 反转规律: 异动后 24h 内的最坏点与最好点\n")
    lines.append("| 指标 | P25 | P50 | P75 |")
    lines.append("|---|---|---|---|")
    for c in ["ret_after_min", "ret_after_max"]:
        s = sub[c]
        lines.append(f"| {c} | {s.quantile(0.25):.1f} | {s.median():.1f} | {s.quantile(0.75):.1f} |")
    lines.append("")

    lines.append("### 3.3 止损规律: 做空后先冲高再回落 vs 直接回落\n")
    sub2 = sub.copy()
    sub2["early_spike"] = sub2.fwd_max_15m > 5
    for g, grp in sub2.groupby("early_spike"):
        lines.append(
            f"- fwd_max_15m {'>5%' if g else '<=5%'} (n={len(grp)}): "
            f"15m回落中位 {grp.ret_after_15m.median():.1f}%, 1h回落中位 {grp.ret_after_1h.median():.1f}%, "
            f"24h回落中位 {grp.ret_after_24h.median():.1f}%"
        )
    lines.append("")

    # 写入报告
    out = "docs/research/SPIKE_ANOMALY_STATS.md"
    with open(out, "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"报告已写入 {out}")
    print("\n".join(lines[:40]))


if __name__ == "__main__":
    main()