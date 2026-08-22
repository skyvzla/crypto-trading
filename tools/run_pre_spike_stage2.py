"""W3 stage2: 预警->spike确认->做空入场的路径级成本模拟。

对照设计:
  A 组: 命中预警条件的时刻 -> 20 根内出现 spike -> spike 下一根 open 做空
  B 组: 全部 spike 事件无条件入场（基线）
SL x HOLD 先触优先, 费用 0.1%。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

ROOT = Path("data/market/candles")
DETAIL = "/tmp/opencode/pre_spike_test_detail.parquet"
ATR_THRESHOLD = 5.0
WICK_THRESHOLD = 4.0
WAIT_BARS = 20
SPIKE_RATIO = 1.15
FEE_SLIP = 0.001
SL_GRID = (0.02, 0.03, 0.05)
HOLD_MINUTES = (15, 30, 60)


def load_hits() -> pd.DataFrame:
    con = duckdb.connect()
    hits = con.execute(
        f"""SELECT symbol, open_ms FROM read_parquet('{DETAIL}')
            WHERE atr_mult > ? AND wick > ?""",
        [ATR_THRESHOLD, WICK_THRESHOLD],
    ).fetchdf()
    con.close()
    return hits


def load_1m(symbol: str, start_ms: int, end_ms: int) -> pd.DataFrame:
    con = duckdb.connect()
    df = con.execute(
        """SELECT epoch_ms(open_time)::BIGINT AS open_ms, open, high, low, close
           FROM read_parquet(?, union_by_name=true) ORDER BY open_time""",
        [str(ROOT / symbol / "1m" / "**" / "*.parquet")],
    ).fetchdf()
    con.close()
    return df[(df["open_ms"] >= start_ms) & (df["open_ms"] < end_ms)].reset_index(drop=True)


def simulate(bars: pd.DataFrame, entry_ms: int, hold_ms: int) -> dict[str, float] | None:
    times = bars["open_ms"].to_numpy(np.int64)
    entry_idx = np.searchsorted(times, entry_ms, side="left")
    if entry_idx >= len(bars):
        return None
    entry = float(bars.iloc[entry_idx]["open"])
    if entry <= 0:
        return None
    end_idx = np.searchsorted(times, int(entry_ms) + hold_ms, side="right")
    seg_h = bars["high"].to_numpy(float)[entry_idx:end_idx] / entry
    seg_l = bars["low"].to_numpy(float)[entry_idx:end_idx]
    results: dict[str, float] = {}
    for sl in SL_GRID:
        stopped_at = np.argmax(seg_h >= 1.0 + sl) if (seg_h >= 1.0 + sl).any() else -1
        for minutes in HOLD_MINUTES:
            key = f"sl{int(sl*100)}_h{minutes}"
            horizon_end = np.searchsorted(times, int(entry_ms) + minutes * 60_000, side="right")
            if horizon_end - entry_idx < 3:
                results[key] = np.nan
                continue
            seg_h_h = bars["high"].to_numpy(float)[entry_idx:horizon_end] / entry
            stop_line = 1.0 + sl
            hit_stop = (seg_h_h >= stop_line)
            stop_pos = int(np.argmax(hit_stop)) if hit_stop.any() else -1
            if stop_pos >= 0 and not (stop_pos == len(seg_h_h) - 1 and seg_h_h[-1] < stop_line):
                results[key] = -sl - FEE_SLIP
                continue
            last_close = float(bars.iloc[horizon_end - 1]["close"]) / entry
            results[key] = (1.0 - last_close) - FEE_SLIP
    return results


def run_group(name: str, events: pd.DataFrame) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for symbol, group in events.groupby("symbol"):
        start_ms = int(group["open_ms"].min()) - 60_000
        end_ms = int(group["open_ms"].max()) + WAIT_BARS * 60_000 + 61 * 60_000
        bars = load_1m(symbol, start_ms, end_ms)
        if bars.empty:
            continue
        h = bars["high"].to_numpy(float)
        c = bars["close"].to_numpy(float)
        prev_c = np.roll(c, 1)
        prev_c[0] = c[0]
        spike_positions = np.where(h >= prev_c * SPIKE_RATIO)[0]
        times = bars["open_ms"].to_numpy(np.int64)
        for ts in group["open_ms"]:
            trig_idx = np.searchsorted(times, int(ts), side="right")
            window = spike_positions[(spike_positions > trig_idx) & (spike_positions <= trig_idx + WAIT_BARS)]
            if len(window) == 0:
                continue
            spike_idx = int(window[0])
            if spike_idx + 1 >= len(bars):
                continue
            entry_ms = int(times[spike_idx + 1])
            outcome = simulate(bars, entry_ms, max(HOLD_MINUTES) * 60_000)
            if outcome is None:
                continue
            rows.append({"group": name, "symbol": symbol, "trigger_ms": int(ts), **outcome})
    return rows


def main() -> int:
    started = time.monotonic()
    hits = load_hits()
    print(f"预警命中时刻: {len(hits)}", flush=True)

    rows_a = run_group("filtered", hits)
    print(f"A组(filtered) 完成交易: {len(rows_a)}", flush=True)

    all_spikes = load_all_spikes()
    rows_b = run_group("baseline", all_spikes)
    print(f"B组(baseline) 完成交易: {len(rows_b)}", flush=True)

    sim = pd.DataFrame(rows_a + rows_b)
    sim.to_parquet("/tmp/opencode/w3_stage2_sim.parquet", index=False)

    lines = ["# W3 Stage2: 预警链路路径模拟（test 段）", ""]
    for name, label in (("filtered", f"A: 预警命中(atr>{ATR_THRESHOLD}x & wick>{WICK_THRESHOLD}%)"), ("baseline", "B: 全部spike无条件")):
        sub = sim[sim["group"] == name]
        lines += [f"## {label}  n={len(sub)}", ""]
        summary_rows = []
        for col in [c for c in sub.columns if c.startswith(("sl",))]:
            values = sub[col].dropna()
            win = (values > 0).mean()
            summary_rows.append({"param": col, "n": len(values), "win": round(win, 3), "avg": round(values.mean(), 4), "med": round(values.median(), 4)})
        lines += [pd.DataFrame(summary_rows).to_string(index=False), ""]
    Path("/tmp/opencode/w3_stage2_report.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"耗时 {time.monotonic()-started:.0f}s -> /tmp/opencode/w3_stage2_report.md")
    return 0


def load_all_spikes() -> pd.DataFrame:
    con = duckdb.connect()
    idx = con.execute(
        "SELECT symbol, relative_path, first_open_ms, last_close_ms "
        "FROM read_parquet('data/market/candles/archive_index.parquet') WHERE timeframe='1m'"
    ).fetchdf()
    root = "data/market/candles/"
    parts: list[pd.DataFrame] = []
    for symbol in sorted(idx["symbol"].unique()):
        paths = [root + rel for rel in idx[idx["symbol"] == symbol]["relative_path"]]
        if not paths:
            continue
        try:
            df = con.execute(
                f"""SELECT epoch_ms(open_time)::BIGINT AS open_ms, high, close
                    FROM read_parquet({paths!r}, union_by_name=true)
                    WHERE epoch_ms(open_time) >= ? AND epoch_ms(open_time) < ?""",
                [int(pd.Timestamp("2026-05-01", tz="UTC").timestamp() * 1000),
                 int(pd.Timestamp("2026-07-31", tz="UTC").timestamp() * 1000)],
            ).fetchdf()
        except Exception:
            continue
        if df.empty:
            continue
        h = df["high"].to_numpy(float)
        c = df["close"].to_numpy(float)
        prev_c = np.roll(c, 1)
        prev_c[0] = c[0]
        mask = h >= prev_c * SPIKE_RATIO
        if mask.any():
            parts.append(df[mask][["symbol" if "symbol" in df else "open_ms"]].assign(symbol=symbol, open_ms=df.loc[mask, "open_ms"]))
    con.close()
    frame = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=["symbol", "open_ms"])
    return frame[["symbol", "open_ms"]]


if __name__ == "__main__":
    sys.exit(main())
