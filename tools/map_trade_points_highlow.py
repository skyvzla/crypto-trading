"""三高三低组合 → 具体买卖点映射。

入场逻辑：upward 异动尾声（osc_end）做空，下一根 1m open 市价入场。
止盈/止损：-5% / +5%（相对入场价），用 osc_end 后 1m 数据按时间顺序判定先后。
窗口：1h 内未触达则按 1h close 平仓（评估超时表现）。

三高 = vwap_dev_5m > P50 且 ema_ratio_5m > P50 且 roc_5m > P50（做空好）
三低 = 三指标 <= P50（做空差，反向组）

输出: reports/trade_points_highlow.csv + 打印挑选的代表事件
读取: 按 symbol 分块（仅读所选事件的 osc_end 后 24h 区间）。
"""
from __future__ import annotations

import os
import sys

import duckdb
import numpy as np
import pandas as pd

MS_MIN = 60_000
MS_HOUR = 60 * MS_MIN
MS_DAY = 24 * MS_HOUR

TAKE_PROFIT = 0.05   # -5% 止盈（回落）
STOP_LOSS = 0.05     # +5% 止损（冲高）
OBS_WINDOW = 24 * MS_HOUR


def parse_ms(iso: str) -> int:
    return int(pd.Timestamp(iso).timestamp() * 1000)


def load_merged() -> pd.DataFrame:
    m = pd.read_csv("reports/spike_anomaly_metrics.csv")
    s = pd.read_csv("reports/spike_anomaly_metrics_1s.csv")
    s = s[["symbol", "event_day", "direction"] + [c for c in s.columns if c.startswith("fwd_max_1s")]]
    df = m.merge(s, on=["symbol", "event_day", "direction"], how="left")
    return df


def main() -> None:
    df = load_merged()
    up = df[df.direction == "upward"].copy()
    up = up[up.ret_after_15m.notna() & up.vwap_dev_5m.notna() & up.ema_ratio_5m.notna() & up.roc_5m.notna()]

    p50_v, p50_e, p50_r = up.vwap_dev_5m.median(), up.ema_ratio_5m.median(), up.roc_5m.median()
    hi = up[(up.vwap_dev_5m > p50_v) & (up.ema_ratio_5m > p50_e) & (up.roc_5m > p50_r)]
    lo = up[(up.vwap_dev_5m <= p50_v) & (up.ema_ratio_5m <= p50_e) & (up.roc_5m <= p50_r)]
    print(f"三高 n={len(hi)} (15m<-5%率 {(hi.ret_after_15m < -5).mean()*100:.0f}%), 三低 n={len(lo)} ({(lo.ret_after_15m < -5).mean()*100:.0f}%)")

    # 挑选代表事件: 正向组取 15m 回落最深/中位/较浅各若干, 反向组取最差(上涨)/中位
    def pick(group: pd.DataFrame, n: int, sort_desc: bool) -> pd.DataFrame:
        g = group.sort_values("ret_after_15m", ascending=not sort_desc)
        picks = []
        for frac in np.linspace(0, 1, n):
            idx = int(frac * (len(g) - 1))
            picks.append(g.iloc[idx])
        return pd.DataFrame(picks).drop_duplicates("symbol")

    hi_picks = pick(hi, 10, True)
    lo_picks = pick(lo, 8, False)

    # 读 1m 数据（按 symbol 分块, 只读 osc_end 后 24h）
    root = "data/market/candles"
    idx = pd.read_parquet(os.path.join(root, "archive_index.parquet"))
    con = duckdb.connect(":memory:")
    con.execute("SET TimeZone='UTC'")
    idx1m = idx[idx["timeframe"] == "1m"]

    selected = pd.concat([hi_picks, lo_picks])
    cache: dict[str, dict[int, tuple]] = {}
    for sym in selected["symbol"].unique():
        sym_ev = selected[selected["symbol"] == sym]
        intervals = [(parse_ms(r.osc_end_utc), parse_ms(r.osc_end_utc) + OBS_WINDOW) for r in sym_ev.itertuples()]
        merged = []
        for t0, t1 in sorted(intervals):
            if merged and t0 <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], t1))
            else:
                merged.append((t0, t1))
        by_time: dict[int, tuple] = {}
        for t0, t1 in merged:
            files = [os.path.join(root, v) for v in idx1m[
                (idx1m["symbol"] == sym) & (idx1m["first_open_ms"] < t1) & (idx1m["last_close_ms"] >= t0)
            ]["relative_path"]]
            if not files:
                continue
            rows = con.execute(
                """SELECT epoch_ms(open_time),open,high,low,close
                   FROM read_parquet(?, union_by_name=true)
                   WHERE symbol=? AND timeframe='1m' AND epoch_ms(open_time)>=? AND epoch_ms(open_time)<?
                   ORDER BY open_time""",
                (files, sym, t0, t1),
            ).fetchall()
            for r in rows:
                by_time[r[0]] = tuple(r[1:])
        cache[sym] = by_time
        print(f"  读入 {sym}")

    # 计算买卖点
    def simulate(row: pd.Series, label: str) -> dict:
        osc_end = parse_ms(row.osc_end_utc)
        by = cache[row.symbol]
        bars = sorted((t for t in by if t >= osc_end))
        if len(bars) < 5:
            return {}
        t0 = bars[0]
        entry = float(by[t0][0])  # 下一根 1m open 市价入场
        tp_price = entry * (1 - TAKE_PROFIT)
        sl_price = entry * (1 + STOP_LOSS)
        tp_t, sl_t = None, None
        for t in bars:
            o, h, l, c = by[t]
            if tp_t is None and l <= tp_price:
                tp_t = t
            if sl_t is None and h >= sl_price:
                sl_t = t
            if tp_t is not None and sl_t is not None:
                break
        t_end = t0 + MS_HOUR
        last = bars[-1]
        close_at_1h = float(by[t_end][3]) if t_end in by else float(by[last][3])
        if tp_t is not None and (sl_t is None or tp_t <= sl_t):
            result, exit_t, exit_price = "止盈-5%", tp_t, tp_price
        elif sl_t is not None:
            result, exit_t, exit_price = "止损+5%", sl_t, sl_price
        else:
            result, exit_t, exit_price = "1h未触发平仓", t_end, close_at_1h
        pnl = (entry - exit_price) / entry * 100
        # 24h 内最深回落/最高冲高
        highs = [float(by[t][2]) for t in bars]
        lows = [float(by[t][3]) for t in bars]
        return {
            "组别": label,
            "symbol": row.symbol,
            "异动区间": f"{row.osc_start_utc[:16]} ~ {row.osc_end_utc[:16]}",
            "入场时间": pd.Timestamp(t0, unit="ms").strftime("%Y-%m-%d %H:%M"),
            "入场价": round(entry, 6),
            "止盈价": round(tp_price, 6),
            "止损价": round(sl_price, 6),
            "1h结果": result,
            "退出时间": pd.Timestamp(exit_t, unit="ms").strftime("%H:%M"),
            "1h盈亏%": round(pnl, 2),
            "24h最坏点%": round(min(lows) / entry * 100 - 100, 1),
            "24h最高点%": round(max(highs) / entry * 100 - 100, 1),
            "vwap_dev": round(row.vwap_dev_5m, 1),
            "ema_ratio": round(row.ema_ratio_5m, 3),
            "roc_5m": round(row.roc_5m, 1),
        }

    out_rows = []
    for r in hi_picks.itertuples():
        out_rows.append(simulate(r, "正向三高"))
    for r in lo_picks.itertuples():
        out_rows.append(simulate(r, "反向三低"))
    out = pd.DataFrame(out_rows)
    out = out[out.symbol.notna()]
    out.to_csv("reports/trade_points_highlow.csv", index=False)
    print(f"\n完成: {len(out)} 事件 -> reports/trade_points_highlow.csv")

    pd.set_option("display.width", 250)
    pd.set_option("display.max_columns", 30)
    print("\n正向组（三高，做空好）:")
    print(out[out.组别 == "正向三高"].to_string(index=False))
    print("\n反向组（三低，做空差）:")
    print(out[out.组别 == "反向三低"].to_string(index=False))


if __name__ == "__main__":
    main()