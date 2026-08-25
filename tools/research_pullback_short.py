#!/usr/bin/env python3
"""研究：3s 暴涨后回落接空的可行性统计。

思路：检测 3s 暴涨（涨幅+放量）事件，等价格从插针高点回落 pullback 后在
起涨点（暴涨窗口起点价）上方接空，验证短持仓均值回归是否有统计优势。

规则：
- 回落触发用 1s bar 的 low 触及回落目标价（贴近限价单成交语义）；
- 挂单价（回落目标价）必须高于起涨点，否则该事件不空；
- 成交价 = max(回落目标价, 触发 bar 的 open)（SELL 限价保守模型）；
- 持仓按止盈/止损/最大持仓时间退出。

数据只读 DuckDB candles 归档（走 archive sidecar index 按 symbol + 时间范围
部分读取 parquet）。输出每 symbol 的事件统计 CSV、逐笔 CSV 与参数网格对比。
"""

from __future__ import annotations

import argparse
import itertools
from dataclasses import dataclass, field
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

MS_1S = 1_000

BAR_COLUMNS = ["open_ms", "open", "high", "low", "close", "volume"]
TAKER_COLUMNS = ["taker_buy_volume", "taker_sell_volume"]


@dataclass
class PullbackEventStats:
    symbol: str = ""
    events: int = 0
    filled: int = 0
    rejected_below_origin: int = 0
    wins: int = 0
    losses: int = 0
    timeouts: int = 0
    gross_pnl_usdt: float = 0.0
    avg_hold_seconds: float = 0.0
    detail_rows: list[dict] = field(default_factory=list)


# 手续费（单边比例）与市价滑点成交模型：
# 入场为限价（maker），止盈为限价回补（maker），止损/超时为市价（taker，
# 止损按触发 bar 的最高价成交，模拟滑点）。
MAKER_FEE = 0.0002
TAKER_FEE = 0.0004


def _select_paths(
    index_path: Path, symbol: str, timeframe: str, start_ms: int, end_ms: int
) -> list[str]:
    con = duckdb.connect(":memory:")
    try:
        rows = con.execute(
            """
            SELECT relative_path FROM read_parquet(?)
            WHERE symbol = ? AND timeframe = ?
              AND first_open_ms < ? AND last_close_ms >= ?
            ORDER BY first_open_ms
            """,
            [str(index_path), symbol, timeframe, end_ms, start_ms],
        ).fetchall()
    finally:
        con.close()
    root = index_path.parent
    return [str(root / path) for path, in rows]


def _select_1s_paths(index_path: Path, symbol: str, start_ms: int, end_ms: int) -> list[str]:
    return _select_paths(index_path, symbol, "1s", start_ms, end_ms)


def _load_1s(index_path: Path, symbol: str, start_ms: int, end_ms: int) -> pd.DataFrame:
    paths = _select_1s_paths(index_path, symbol, start_ms, end_ms)
    if not paths:
        return pd.DataFrame(columns=BAR_COLUMNS + TAKER_COLUMNS)
    con = duckdb.connect(":memory:")
    try:
        df = con.execute(
            """
            SELECT epoch_ms(open_time) AS open_ms, open, high, low, close, volume,
                   taker_buy_volume, taker_sell_volume
            FROM read_parquet(?, union_by_name=true)
            WHERE symbol = ? AND epoch_ms(open_time) >= ? AND epoch_ms(open_time) < ?
            ORDER BY open_time
            """,
            [paths, symbol, start_ms, end_ms],
        ).fetchdf()
    finally:
        con.close()
    for col in BAR_COLUMNS + TAKER_COLUMNS:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["open_ms", "close"]).reset_index(drop=True)
    df["open_ms"] = df["open_ms"].astype(np.int64)
    return df


def _load_1m(index_path: Path, symbol: str, start_ms: int, end_ms: int) -> pd.DataFrame:
    """加载 1m K 线（起涨背景过滤用），只取 low 与 open_ms。"""
    paths = _select_paths(index_path, symbol, "1m", start_ms, end_ms)
    if not paths:
        return pd.DataFrame(columns=["open_ms", "low"])
    con = duckdb.connect(":memory:")
    try:
        df = con.execute(
            """
            SELECT epoch_ms(open_time) AS open_ms, low
            FROM read_parquet(?, union_by_name=true)
            WHERE symbol = ? AND epoch_ms(open_time) >= ? AND epoch_ms(open_time) < ?
            ORDER BY open_time
            """,
            [paths, symbol, start_ms, end_ms],
        ).fetchdf()
    finally:
        con.close()
    df["open_ms"] = pd.to_numeric(df["open_ms"], errors="coerce").astype(np.int64)
    df["low"] = pd.to_numeric(df["low"], errors="coerce")
    return df.dropna(subset=["open_ms", "low"]).reset_index(drop=True)


def _filter_rise_duration(
    df_1m: pd.DataFrame,
    signal_ms_arr: np.ndarray,
    *,
    lookback_ms: int,
    min_duration_ms: int,
) -> np.ndarray:
    """保留满足「起涨周期」的事件：过去 lookback 窗口内最低 1m 低点
    距信号 >= min_duration_ms（即该低点在更早之前，上涨已持续足够久）。
    返回布尔掩码。lookback_ms<=0 时不做过滤。
    """
    if lookback_ms <= 0 or df_1m.empty:
        return np.ones(len(signal_ms_arr), dtype=bool)
    om = df_1m["open_ms"].to_numpy(np.int64)
    lo = df_1m["low"].to_numpy(float)
    starts = np.searchsorted(om, signal_ms_arr - lookback_ms, side="left")
    ends = np.searchsorted(om, signal_ms_arr, side="left")
    keep = np.zeros(len(signal_ms_arr), dtype=bool)
    for i, (s0, e0) in enumerate(zip(starts, ends, strict=True)):
        if e0 - s0 < 2:
            continue
        window = lo[s0:e0]
        low_time = om[s0 + int(np.argmin(window))]
        if signal_ms_arr[i] - low_time >= min_duration_ms:
            keep[i] = True
    return keep


def _detect_3s_spikes(
    df: pd.DataFrame,
    *,
    rise_threshold: float,
    vol_multiple: float,
    cooldown_seconds: int,
    baseline_seconds: int = 60,
) -> np.ndarray:
    """返回命中 3s 暴涨的 1s bar 下标（已按冷却去重）。

    条件：
    - 窗口连续：open_ms 与 3s/60s 前严格相差 3s/60s；
    - 3s 涨幅 close[i]/close[i-3] - 1 >= rise_threshold；
    - 3s 成交量 sum(volume[i-2..i]) >= vol_multiple × 中位数(volume[i-60..i-1]) × 3。
    """
    n = len(df)
    if n < baseline_seconds + 3:
        return np.array([], dtype=np.int64)
    open_ms = df["open_ms"].to_numpy(np.int64)
    close = df["close"].to_numpy(float)
    volume = df["volume"].to_numpy(float)

    continuous = np.zeros(n, dtype=bool)
    continuous[baseline_seconds:] = (
        (open_ms[baseline_seconds:] - open_ms[baseline_seconds - 3 : n - 3]) == 3 * MS_1S
    ) & (open_ms[baseline_seconds:] - open_ms[: n - baseline_seconds] == baseline_seconds * MS_1S)

    rise = np.full(n, np.nan)
    rise[baseline_seconds:] = (
        close[baseline_seconds:] / close[baseline_seconds - 3 : n - 3] - 1.0
    )

    vol_3s = np.full(n, np.nan)
    vol_3s[baseline_seconds:] = (
        volume[baseline_seconds:]
        + volume[baseline_seconds - 1 : n - 1]
        + volume[baseline_seconds - 2 : n - 2]
    )
    median_vol = pd.Series(volume).rolling(baseline_seconds, min_periods=baseline_seconds).median().to_numpy()

    hit = (
        continuous
        & (rise >= rise_threshold)
        & (vol_3s >= vol_multiple * np.maximum(median_vol, 1e-12) * 3.0)
        & np.isfinite(vol_3s)
    )

    idx = np.flatnonzero(hit)
    if idx.size == 0:
        return idx
    kept = [idx[0]]
    for i in idx[1:]:
        if open_ms[i] - open_ms[kept[-1]] >= cooldown_seconds * MS_1S:
            kept.append(i)
    return np.array(kept, dtype=np.int64)


def _simulate_trades(
    df: pd.DataFrame,
    event_idx: list[int],
    *,
    pullback: float,
    take_profit: float,
    stop_loss: float,
    max_hold_seconds: int,
    wait_seconds: int,
    circuit: str = "none",
    circuit_fill: str = "high",
    min_spike_rise: float = 0.0,
    retrace_frac: float = 0.0,
    stop_5m_high: bool = True,
    stop_15m_loss: bool = True,
    drawdown_peak: float = 0.20,
    drawdown_ratio: float = 0.10,
) -> list[dict]:
    open_ms = df["open_ms"].to_numpy(np.int64)
    open_p = df["open"].to_numpy(float)
    high = df["high"].to_numpy(float)
    low = df["low"].to_numpy(float)
    close = df["close"].to_numpy(float)
    buy_vol = (
        df["taker_buy_volume"].to_numpy(float)
        if "taker_buy_volume" in df
        else np.full(len(df), np.nan)
    )
    sell_vol = (
        df["taker_sell_volume"].to_numpy(float)
        if "taker_sell_volume" in df
        else np.full(len(df), np.nan)
    )

    def buy_ratio(lo: int, hi: int) -> float | None:
        """[lo, hi) 区间内主动买量占比；数据缺失或总量为 0 时返回 None。"""
        if lo < 0:
            lo = 0
        if hi > n:
            hi = n
        if hi <= lo:
            return None
        b = buy_vol[lo:hi]
        s = sell_vol[lo:hi]
        if not np.isfinite(b).all() or not np.isfinite(s).all():
            return None
        total = float(np.nansum(b) + np.nansum(s))
        if total <= 0:
            return None
        return float(np.nansum(b)) / total

    n = len(df)
    rows = []
    for start in event_idx:
        origin_price = close[start - 3]
        if not np.isfinite(origin_price) or origin_price <= 0:
            continue
        spike_high = 0.0
        entry_price = None
        entry_idx = None
        reason = "skipped_no_retrace"
        for j in range(start, n):
            if open_ms[j] - open_ms[start] > wait_seconds * MS_1S:
                break
            spike_high = max(spike_high, high[j])
            # 价格跌破起涨点 → 插针失效，放弃（不能低于起涨点空）
            if low[j] < origin_price:
                reason = "invalid_below_origin"
                break
            # 大插针确认：插针总涨幅需达到门槛后才考虑回落接空
            if spike_high < origin_price * (1.0 + min_spike_rise):
                continue
            # 挂单价：回吐插针涨幅 retrace_frac（默认），否则从高点回落 pullback
            if retrace_frac > 0:
                candidate = spike_high - retrace_frac * (spike_high - origin_price)
            else:
                candidate = spike_high * (1.0 - pullback)
            if candidate > origin_price and low[j] <= candidate:
                entry_price = max(candidate, open_p[j])
                entry_idx = j
                break
        if entry_price is None or entry_idx is None:
            rows.append({
                "symbol": None,
                "signal_ms": int(open_ms[start]),
                "origin_price": origin_price,
                "spike_high": spike_high,
                "entry_ms": None,
                "entry_price": None,
                "reason": reason,
                "return_pct": 0.0,
                "hold_seconds": None,
            })
            continue
        entry_ms = open_ms[entry_idx]
        tp_price = entry_price * (1.0 - take_profit)
        sl_price = entry_price * (1.0 + stop_loss)
        circuit_price = spike_high if circuit == "spike_high" else None
        peak_price = entry_price
        peak_return = 0.0
        exit_price = None
        exit_ms = None
        reason = "timeout"
        for j in range(entry_idx + 1, n):
            elapsed = open_ms[j] - entry_ms
            # 熔断优先：重新涨破插针高点 → 插针延续，市价平仓
            # circuit_fill=high 最坏（bar 最高价成交）；close 中性（检测后当根收盘价）
            if circuit_price is not None and high[j] >= circuit_price:
                exit_price = high[j] if circuit_fill == "high" else close[j]
                exit_ms = open_ms[j]
                reason = "circuit"
                break
            # 5m 插针高点止损：持仓>5min 且重新触及插针高点 → 市价止损（前5分钟保护期）
            if stop_5m_high and elapsed > 300_000 and high[j] >= spike_high:
                exit_price = high[j]
                exit_ms = open_ms[j]
                reason = "stop_5m_high"
                break
            # 浮盈峰值跟踪（空单：价格越低浮盈越大）
            if low[j] < peak_price:
                peak_price = low[j]
            peak_return = (entry_price - peak_price) / entry_price
            # v2.2 浮盈回撤止盈：峰值浮盈达到 drawdown_peak 后，从峰值回撤 drawdown_ratio
            if (
                drawdown_peak > 0
                and peak_price > 0
                and peak_return >= drawdown_peak
                and (close[j] - peak_price) / peak_price >= drawdown_ratio
            ):
                exit_price = close[j]
                exit_ms = open_ms[j]
                reason = "profit_drawdown"
                break
            if low[j] <= tp_price:
                exit_price = tp_price
                exit_ms = open_ms[j]
                reason = "take_profit"
                break
            if circuit_price is None and stop_loss > 0 and high[j] >= sl_price:
                # 市价止损：按触发 bar 最高价成交（模拟滑点）
                exit_price = high[j]
                exit_ms = open_ms[j]
                reason = "stop_loss"
                break
            # 15m 时间止损（参考 candidate-v1 time_risk）：持仓≥15min 且做空净亏损（价>entry）
            if stop_15m_loss and elapsed >= 900_000 and close[j] >= entry_price:
                exit_price = close[j]
                exit_ms = open_ms[j]
                reason = "stop_15m_loss"
                break
            if elapsed >= max_hold_seconds * MS_1S:
                exit_price = close[j]
                exit_ms = open_ms[j]
                reason = "timeout"
                break
        if exit_price is None:
            exit_price = close[-1]
            exit_ms = int(open_ms[-1])
            reason = "timeout"
        gross_ret = (entry_price - exit_price) / entry_price
        # 入场 maker + 出场 maker(止盈)/taker(熔断/止损/超时)
        fee_rate = MAKER_FEE if reason == "take_profit" else TAKER_FEE
        net_ret = gross_ret - MAKER_FEE - fee_rate
        rows.append({
            "symbol": None,
            "signal_ms": int(open_ms[start]),
            "origin_price": origin_price,
            "spike_high": spike_high,
            "entry_ms": int(entry_ms),
            "entry_price": entry_price,
            "exit_ms": int(exit_ms),
            "exit_price": exit_price,
            "reason": reason,
            "return_pct": net_ret,
            "gross_return_pct": gross_ret,
            "hold_seconds": (exit_ms - entry_ms) / 1000.0,
            "buy_ratio_spike": buy_ratio(start, entry_idx),
            "buy_ratio_entry": buy_ratio(entry_idx - 10, entry_idx),
            "buy_ratio_hold": buy_ratio(entry_idx, entry_idx + 10),
        })
    return rows


def run_symbol(
    df: pd.DataFrame,
    event_idx: np.ndarray,
    symbol: str,
    *,
    pullback: float,
    take_profit: float,
    stop_loss: float,
    max_hold_seconds: int,
    wait_seconds: int,
    circuit: str,
    circuit_fill: str,
    min_spike_rise: float,
    retrace_frac: float,
    stop_5m_high: bool,
    stop_15m_loss: bool,
    drawdown_peak: float,
    drawdown_ratio: float,
    notional: float,
) -> PullbackEventStats:
    stats = PullbackEventStats(symbol=symbol)
    if df.empty:
        return stats
    stats.events = len(event_idx)
    trades = _simulate_trades(
        df,
        event_idx.tolist(),
        pullback=pullback,
        take_profit=take_profit,
        stop_loss=stop_loss,
        max_hold_seconds=max_hold_seconds,
        wait_seconds=wait_seconds,
        circuit=circuit,
        circuit_fill=circuit_fill,
        min_spike_rise=min_spike_rise,
        retrace_frac=retrace_frac,
        stop_5m_high=stop_5m_high,
        stop_15m_loss=stop_15m_loss,
        drawdown_peak=drawdown_peak,
        drawdown_ratio=drawdown_ratio,
    )
    for row in trades:
        row["symbol"] = symbol
        stats.detail_rows.append(row)
        if row["reason"] == "invalid_below_origin":
            stats.rejected_below_origin += 1
            continue
        if row["reason"] == "skipped_no_retrace":
            continue
        stats.filled += 1
        if row["reason"] in {"take_profit", "profit_drawdown"}:
            stats.wins += 1
        elif row["reason"] in {"stop_loss", "circuit", "stop_5m_high", "stop_15m_loss"}:
            stats.losses += 1
        else:
            stats.timeouts += 1
        stats.gross_pnl_usdt += row["return_pct"] * notional
        if row["hold_seconds"] is not None:
            stats.avg_hold_seconds += row["hold_seconds"]
    if stats.filled:
        stats.avg_hold_seconds /= stats.filled
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description="3s 暴涨后回落接空可行性统计")
    parser.add_argument("--index", type=Path, default="data/market/candles/archive_index.parquet")
    parser.add_argument("--symbols", nargs="+", required=True)
    parser.add_argument("--start", required=True, help="ISO 时间")
    parser.add_argument("--end", required=True, help="ISO 时间")
    parser.add_argument("--output", type=Path, default="reports/research_pullback_short")
    parser.add_argument("--rise-threshold", type=float, default=0.03)
    parser.add_argument("--vol-multiple", type=float, default=3.0)
    parser.add_argument("--cooldown-seconds", type=int, default=180)
    parser.add_argument("--pullback", type=float, nargs="+", default=[0.05])
    parser.add_argument("--take-profit", type=float, nargs="+", default=[0.01])
    parser.add_argument("--stop-loss", type=float, nargs="+", default=[0.005])
    parser.add_argument("--max-hold-seconds", type=int, nargs="+", default=[120])
    parser.add_argument("--wait-seconds", type=int, default=300)
    parser.add_argument("--circuit", type=str, default="none",
                        choices=["none", "spike_high"],
                        help="熔断退出：spike_high=重新涨破插针高点即市价平仓；none=固定止损")
    parser.add_argument("--circuit-fill", type=str, default="high",
                        choices=["high", "close"],
                        help="熔断成交价：high=按触发bar最高价（最坏滑点）；close=按当根收盘价（中性）")
    parser.add_argument("--min-spike-rise", type=float, default=0.0,
                        help="插针总涨幅门槛（spike_high/origin-1），如 0.30 表示只做大插针")
    parser.add_argument("--retrace-frac", type=float, nargs="+", default=[0.35],
                        help="挂单价=回吐插针涨幅的比例（如0.35=回落35%涨幅处挂空）；>0时覆盖 --pullback")
    parser.add_argument("--rise-low-lookback-hours", type=float, default=0.0,
                        help="起涨背景回看窗口（小时）；0=关闭。找该窗口内最低1m低点，要求其距信号>=min-rise-duration")
    parser.add_argument("--min-rise-duration-hours", type=float, default=6.0,
                        help="起涨周期最短时长（小时）：窗口内最低低点距信号必须 >= 该值（如6h=过去6h持续上涨）")
    parser.add_argument("--no-stop-5m-high", action="store_true",
                        help="关闭 5m 插针高点止损（持仓>5min 且触及插针高点）")
    parser.add_argument("--no-stop-15m-loss", action="store_true",
                        help="关闭 15m 时间止损（持仓>=15min 且净亏损）")
    parser.add_argument("--drawdown-peak", type=float, default=0.20,
                        help="v2.2 浮盈回撤止盈：峰值浮盈达到该比例（如0.20）后启用回撤保护")
    parser.add_argument("--drawdown-ratio", type=float, default=0.10,
                        help="v2.2 浮盈回撤止盈：从峰值回撤该比例（如0.10）触发止盈")
    parser.add_argument("--notional", type=float, default=1000.0)
    args = parser.parse_args()

    start_ms = int(pd.Timestamp(args.start).timestamp() * 1000)
    end_ms = int(pd.Timestamp(args.end).timestamp() * 1000)
    args.output.mkdir(parents=True, exist_ok=True)

    grid = list(itertools.product(args.retrace_frac, args.take_profit, args.stop_loss, args.max_hold_seconds))
    all_rows: list[dict] = []
    combos: list[dict] = []

    rise_lookback_ms = int(args.rise_low_lookback_hours * 3_600_000)
    rise_min_dur_ms = int(args.min_rise_duration_hours * 3_600_000)

    symbol_data: dict[str, tuple[pd.DataFrame, np.ndarray, int]] = {}
    for symbol in args.symbols:
        df = _load_1s(args.index, symbol, start_ms, end_ms)
        event_idx = (
            _detect_3s_spikes(
                df,
                rise_threshold=args.rise_threshold,
                vol_multiple=args.vol_multiple,
                cooldown_seconds=args.cooldown_seconds,
            )
            if not df.empty
            else np.array([], dtype=np.int64)
        )
        rejected_rise = 0
        if rise_lookback_ms > 0 and len(event_idx):
            df_1m = _load_1m(
                args.index, symbol, start_ms - rise_lookback_ms, end_ms
            )
            keep = _filter_rise_duration(
                df_1m,
                df["open_ms"].to_numpy(np.int64)[event_idx],
                lookback_ms=rise_lookback_ms,
                min_duration_ms=rise_min_dur_ms,
            )
            rejected_rise = int((~keep).sum())
            event_idx = event_idx[keep]
        symbol_data[symbol] = (df, event_idx, rejected_rise)
        print(f"{symbol}: rows={len(df)} events={len(event_idx)} rejected_rise={rejected_rise}")
        print(f"{symbol}: rows={len(df)} events={len(event_idx)}")

    for symbol in args.symbols:
        df, event_idx, rejected_rise = symbol_data[symbol]
        for rf, tp, sl, hold in grid:
            stats = run_symbol(
                df, event_idx, symbol,
                pullback=0.0,
                take_profit=tp,
                stop_loss=sl,
                max_hold_seconds=hold,
                wait_seconds=args.wait_seconds,
                circuit=args.circuit,
                circuit_fill=args.circuit_fill,
                min_spike_rise=args.min_spike_rise,
                retrace_frac=rf,
                stop_5m_high=not args.no_stop_5m_high,
                stop_15m_loss=not args.no_stop_15m_loss,
                drawdown_peak=args.drawdown_peak,
                drawdown_ratio=args.drawdown_ratio,
                notional=args.notional,
            )
            combo = {
                "symbol": symbol,
                "retrace_frac": rf,
                "pullback": 0.0,
                "take_profit": tp,
                "stop_loss": sl,
                "max_hold_seconds": hold,
                "circuit": args.circuit,
                "min_spike_rise": args.min_spike_rise,
                "rejected_rise": rejected_rise,
                "events": stats.events,
                "filled": stats.filled,
                "rejected_below_origin": stats.rejected_below_origin,
                "skipped_no_retrace": stats.events - stats.filled - stats.rejected_below_origin,
                "wins": stats.wins,
                "losses": stats.losses,
                "timeouts": stats.timeouts,
                "win_rate": stats.wins / stats.filled if stats.filled else 0.0,
                "gross_pnl_usdt": round(stats.gross_pnl_usdt, 2),
                "avg_hold_seconds": round(stats.avg_hold_seconds, 1),
            }
            combos.append(combo)
            for row in stats.detail_rows:
                row.update({
                    "symbol": symbol,
                    "retrace_frac": rf,
                    "pullback": 0.0,
                    "take_profit": tp,
                    "stop_loss": sl,
                    "max_hold_seconds": hold,
                    "circuit": args.circuit,
                })
                all_rows.append(row)

    combo_df = pd.DataFrame(combos)
    combo_df.to_csv(args.output / "summary.csv", index=False)
    if all_rows:
        pd.DataFrame(all_rows).to_csv(args.output / "trades.csv", index=False)

    agg = (
        combo_df.groupby(["retrace_frac", "take_profit", "stop_loss", "max_hold_seconds", "circuit", "min_spike_rise"])
        .agg(
            events=("events", "sum"),
            filled=("filled", "sum"),
            rejected_below_origin=("rejected_below_origin", "sum"),
            rejected_rise=("rejected_rise", "sum"),
            skipped_no_retrace=("skipped_no_retrace", "sum"),
            wins=("wins", "sum"),
            losses=("losses", "sum"),
            timeouts=("timeouts", "sum"),
            gross_pnl_usdt=("gross_pnl_usdt", "sum"),
        )
        .reset_index()
    )
    agg["win_rate"] = agg["wins"] / agg["filled"].replace(0, np.nan)
    agg = agg.sort_values("gross_pnl_usdt", ascending=False)
    agg.to_csv(args.output / "grid_agg.csv", index=False)
    print("\n=== 参数网格汇总（按总收益排序）===")
    print(agg.to_string(index=False))
    print(f"\n输出: {args.output}")


if __name__ == "__main__":
    main()
