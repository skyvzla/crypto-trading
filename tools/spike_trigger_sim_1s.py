"""
1s 粒度触发模拟器：复刻 spike 策略核心触发判定（研究口径）
- rise_5s >= 5%（当前秒 close vs 5 秒前 close）
- 量能: sum(vol[-5:]) / (median(vol[-60:-1]) * 5) >= 3
- 连续性: 5s/60s 前 bar 恰好对齐，缺口放弃
- rise_from_12h_low >= 20%（已完成 1m 低点，前缀最小）
- 冷却 180s
- 在 osc_start ~ osc_end 区间内取首个触发点
逐 symbol 流式处理，峰值内存 = 单币数据。
输出: reports/spike_triggers_1s.csv
"""
import pandas as pd
import numpy as np
import duckdb
import os
import time

ROOT = "data/market/candles"
OUT = "reports/spike_triggers_1s.csv"


def parse_ms(iso: str) -> int:
    return int(pd.Timestamp(iso).timestamp() * 1000)


def scan_events(
    sym: str,
    events: pd.DataFrame,
    s_ts: np.ndarray,
    s_close: np.ndarray,
    s_vol: np.ndarray,
    m_ts: np.ndarray,
    m_low: np.ndarray,
) -> list[dict]:
    """对单个 symbol 的 1s/1m 数组扫描所有事件的触发点。"""
    # 1m 前缀最小低点（index i 对应 m_ts[i-1] 及之前的 min low，即"已完成 1m"低点）
    pref_min = np.minimum.accumulate(m_low)
    recs = []
    for ev in events.itertuples():
        osc_start = parse_ms(ev.osc_start_utc)
        osc_end = parse_ms(ev.osc_end_utc)
        lo = np.searchsorted(s_ts, osc_start)
        hi = np.searchsorted(s_ts, osc_end, side="right")
        if hi - lo < 61:
            continue
        t = s_ts[lo:hi]
        c = s_close[lo:hi]
        v = s_vol[lo:hi]

        # 连续性缺口：diff != 1000；缺口位置后 60s 内不可触发
        invalid = np.zeros(len(t), dtype=bool)
        if len(t) > 1:
            d = np.diff(t)
            for i in np.nonzero(d != 1000)[0]:
                invalid[max(0, i - 59):i + 1] = True

        # rise_5s
        rise = np.full(len(t), np.nan)
        rise[5:] = c[5:] / c[:-5] - 1.0

        # 量能倍数（窗口滚动）
        vol_mult = np.full(len(t), np.nan)
        n = len(t)
        for i in range(60, n):
            bl = np.median(v[i - 60:i])
            if bl > 0:
                vol_mult[i] = v[i - 4:i + 1].sum() / (bl * 5)

        trig = None
        last_sig = -10**18
        for i in range(60, n):
            if t[i] < osc_start + 60_000:
                continue
            if invalid[i] or np.isnan(rise[i]) or rise[i] < 0.05:
                continue
            if np.isnan(vol_mult[i]) or vol_mult[i] < 3.0:
                continue
            if t[i] - last_sig < 180_000:
                continue
            # 12h 低点（已完成 1m，t[i] 所在分钟之前）
            minute_start = t[i] - (t[i] % 60_000)
            k = np.searchsorted(m_ts, minute_start - 60_000, side="right") - 1
            if k < 0 or pref_min[k] <= 0:
                continue
            if c[i] / pref_min[k] - 1.0 < 0.20:
                continue
            trig = i
            last_sig = t[i]
            break

        if trig is None:
            continue
        trig_t = int(t[trig])
        trig_p = float(c[trig])
        t_max = osc_end + 600_000
        f_lo = trig  # 同数组内
        f_hi = np.searchsorted(s_ts, t_max, side="right") - lo
        fut = c[trig:f_hi] if f_hi > trig else np.array([trig_p])
        peak = float(fut.max())
        recs.append(
            {
                "symbol": sym,
                "osc_start_utc": ev.osc_start_utc,
                "osc_end_utc": ev.osc_end_utc,
                "trig_t_ms": trig_t,
                "trig_price": trig_p,
                "peak_price": peak,
                "total_up_pct": (peak / trig_p - 1) * 100,
                "rise_5s_pct": float(rise[trig]) * 100,
                "vol_mult": float(vol_mult[trig]),
                "atr_ratio_5m": ev.atr_ratio_5m,
            }
        )
    return recs


def main() -> None:
    t_start = time.time()
    m = pd.read_csv("reports/spike_anomaly_metrics.csv")
    up = m[m.direction == "upward"].copy()
    up = up[
        up.ret_after_4h.notna()
        & up.vwap_dev_5m.notna()
        & up.ema_ratio_5m.notna()
        & up.roc_5m.notna()
        & up.atr_ratio_5m.notna()
    ]
    p50_v, p50_e, p50_r = (
        up.vwap_dev_5m.median(),
        up.ema_ratio_5m.median(),
        up.roc_5m.median(),
    )
    hi = up[
        (up.vwap_dev_5m > p50_v) & (up.ema_ratio_5m > p50_e) & (up.roc_5m > p50_r)
    ].copy()
    print(f"三高事件: {len(hi)}, symbol: {hi.symbol.nunique()}")

    idx = pd.read_parquet(os.path.join(ROOT, "archive_index.parquet"))
    i1s = idx[idx.timeframe == "1s"]
    i1m = idx[idx.timeframe == "1m"]
    con = duckdb.connect(":memory:")

    all_recs = []
    n_miss = 0
    for si, sym in enumerate(hi["symbol"].unique()):
        ev = hi[hi["symbol"] == sym]
        t0 = min(parse_ms(r.osc_start_utc) - 3600_000 for r in ev.itertuples())
        t1 = max(parse_ms(r.osc_end_utc) + 600_000 for r in ev.itertuples())

        files1s = [
            os.path.join(ROOT, v)
            for v in i1s[
                (i1s["symbol"] == sym)
                & (i1s["first_open_ms"] < t1)
                & (i1s["last_close_ms"] >= t0)
            ]["relative_path"]
        ]
        if files1s:
            rows = con.execute(
                """SELECT epoch_ms(open_time), close, volume FROM read_parquet(?, union_by_name=true)
                   WHERE symbol=? AND timeframe='1s' AND epoch_ms(open_time)>=? AND epoch_ms(open_time)<?
                   ORDER BY open_time""",
                (files1s, sym, t0, t1),
            ).fetchall()
            s_ts = np.array([r[0] for r in rows], dtype=np.int64)
            s_close = np.array([r[1] for r in rows], dtype=np.float64)
            s_vol = np.array([r[2] for r in rows], dtype=np.float64)
        else:
            s_ts = np.empty(0, dtype=np.int64)
            s_close = np.empty(0)
            s_vol = np.empty(0)

        t0m = t0 - 12 * 3600_000
        files1m = [
            os.path.join(ROOT, v)
            for v in i1m[
                (i1m["symbol"] == sym)
                & (i1m["first_open_ms"] < t1)
                & (i1m["last_close_ms"] >= t0m)
            ]["relative_path"]
        ]
        if files1m:
            rows = con.execute(
                """SELECT epoch_ms(open_time), low FROM read_parquet(?, union_by_name=true)
                   WHERE symbol=? AND timeframe='1m' AND epoch_ms(open_time)>=? AND epoch_ms(open_time)<?
                   ORDER BY open_time""",
                (files1m, sym, t0m, t1),
            ).fetchall()
            m_ts = np.array([r[0] for r in rows], dtype=np.int64)
            m_low = np.array([r[1] for r in rows], dtype=np.float64)
        else:
            m_ts = np.empty(0, dtype=np.int64)
            m_low = np.empty(0)

        recs = scan_events(sym, ev, s_ts, s_close, s_vol, m_ts, m_low)
        n_miss += len(ev) - len(recs)
        all_recs.extend(recs)
        if si % 20 == 0 or si == hi["symbol"].nunique() - 1:
            print(
                f"[{si+1}/{hi['symbol'].nunique()}] {sym}: 事件 {len(ev)}, 触发 {len(recs)}, "
                f"累计 {len(all_recs)}/{len(hi)}, {time.time()-t_start:.0f}s"
            )
        del s_ts, s_close, s_vol, m_ts, m_low

    df = pd.DataFrame(all_recs)
    df.to_csv(OUT, index=False)
    print(f"\n触发成功: {len(df)} / {len(hi)}  (未触发 {n_miss})")
    print(f"耗时: {time.time()-t_start:.0f}s")
    print(
        df[["symbol", "trig_t_ms", "trig_price", "total_up_pct", "rise_5s_pct", "vol_mult"]]
        .head(10)
        .to_string()
    )
    print(
        f"\ntotal_up_pct: 中位 {df.total_up_pct.median():.2f}%, 平均 {df.total_up_pct.mean():.2f}%, "
        f"P75 {df.total_up_pct.quantile(.75):.2f}%, P90 {df.total_up_pct.quantile(.9):.2f}%"
    )


if __name__ == "__main__":
    main()