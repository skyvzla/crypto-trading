"""Offline calibration for the unconfirmed spike-short exit rules.

The calculations in this module are research candidates for D-016 through D-020
and D-025.  They deliberately do not expose an execution-policy interface.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import duckdb
import numpy as np
import pandas as pd


@dataclass(frozen=True)
class CalibrationConfig:
    symbol: str = "AKEUSDT"
    study_start_ms: int = 1_782_864_000_000  # 2026-07-01 00:00:00 UTC
    study_end_ms: int = 1_785_542_400_000  # 2026-08-01 00:00:00 UTC
    bar1s_time_shift_ms: int = 0
    fast_slope_bars: int = 5
    slow_slope_bars: int = 15
    volatility_bars: int = 30
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    adx_bars: int = 14
    momentum_change_bars: int = 5
    channel_5m_bars: int = 12
    channel_15m_bars: int = 8
    channel_width_sigma: float = 1.5
    stable_closes: int = 2
    snapshot_minutes: tuple[int, ...] = (0, 5, 15, 30, 60)
    entry_snapshot_seconds: tuple[int, ...] = (90, 300, 900, 3600)


def _utc_text(timestamp_ms: int | float | None) -> str | None:
    if timestamp_ms is None or pd.isna(timestamp_ms):
        return None
    return datetime.fromtimestamp(float(timestamp_ms) / 1000, timezone.utc).isoformat()


def _rolling_log_slope(values: pd.Series, window: int) -> pd.Series:
    x = np.arange(window, dtype=float)
    centered = x - x.mean()
    denominator = float(np.square(centered).sum())
    logs = np.log(values.astype(float))
    return logs.rolling(window, min_periods=window).apply(
        lambda y: float(np.dot(centered, y) / denominator), raw=True
    )


def momentum_indicators(candles: pd.DataFrame, config: CalibrationConfig) -> pd.DataFrame:
    """Calculate causal 1m momentum candidates from completed candles."""
    frame = candles.sort_values("available_ms").reset_index(drop=True).copy()
    close = frame["close"].astype(float)
    log_close = np.log(close)
    returns = log_close.diff()
    volatility = returns.rolling(
        config.volatility_bars, min_periods=config.volatility_bars
    ).std(ddof=0).replace(0, np.nan)
    frame["fast_log_slope_z"] = _rolling_log_slope(
        close, config.fast_slope_bars
    ) / volatility
    frame["slow_log_slope_z"] = _rolling_log_slope(
        close, config.slow_slope_bars
    ) / volatility

    fast_ema = log_close.ewm(span=config.macd_fast, adjust=False).mean()
    slow_ema = log_close.ewm(span=config.macd_slow, adjust=False).mean()
    macd = fast_ema - slow_ema
    signal = macd.ewm(span=config.macd_signal, adjust=False).mean()
    frame["macd_hist_bps"] = (macd - signal) * 10_000
    frame["macd_hist_change_bps"] = frame["macd_hist_bps"].diff(
        config.momentum_change_bars
    )

    high = frame["high"].astype(float)
    low = frame["low"].astype(float)
    previous_close = close.shift(1)
    true_range = pd.concat(
        [
            high - low,
            (high - previous_close).abs(),
            (low - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = pd.Series(
        np.where((up_move > down_move) & (up_move > 0), up_move, 0.0),
        index=frame.index,
    )
    minus_dm = pd.Series(
        np.where((down_move > up_move) & (down_move > 0), down_move, 0.0),
        index=frame.index,
    )
    alpha = 1 / config.adx_bars
    atr = true_range.ewm(alpha=alpha, adjust=False, min_periods=config.adx_bars).mean()
    plus_di = 100 * plus_dm.ewm(
        alpha=alpha, adjust=False, min_periods=config.adx_bars
    ).mean() / atr
    minus_di = 100 * minus_dm.ewm(
        alpha=alpha, adjust=False, min_periods=config.adx_bars
    ).mean() / atr
    denominator = (plus_di + minus_di).replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / denominator
    frame["adx"] = dx.ewm(
        alpha=alpha, adjust=False, min_periods=config.adx_bars
    ).mean()
    frame["plus_di"] = plus_di
    frame["minus_di"] = minus_di
    frame["adx_change"] = frame["adx"].diff(config.momentum_change_bars)
    frame["minus_di_change"] = frame["minus_di"].diff(
        config.momentum_change_bars
    )

    both_down = (frame["fast_log_slope_z"] < 0) & (
        frame["slow_log_slope_z"] < 0
    )
    frame["down_speed_ratio"] = np.where(
        both_down,
        frame["fast_log_slope_z"].abs()
        / frame["slow_log_slope_z"].abs().replace(0, np.nan),
        np.nan,
    )
    # These booleans are comparison probes, not production decisions.
    frame["slope_decay_probe"] = both_down & (frame["down_speed_ratio"] <= 0.5)
    frame["macd_recovery_probe"] = frame["macd_hist_change_bps"] > 0
    frame["adx_di_decay_probe"] = (
        (frame["minus_di"] > frame["plus_di"])
        & (frame["minus_di_change"] < 0)
        & (frame["adx_change"] < 0)
    )
    frame["decay_probe_agreement"] = frame[
        ["slope_decay_probe", "macd_recovery_probe", "adx_di_decay_probe"]
    ].sum(axis=1)
    return frame


def channel_breakout_candidates(
    candles: pd.DataFrame,
    *,
    lookback: int,
    width_sigma: float,
    stable_closes: int,
) -> pd.DataFrame:
    """Return causal descending-channel upper-break candidates.

    Each candle is tested against a log-high regression fitted only on the
    preceding ``lookback`` completed candles.  Once a close breaks a descending
    channel, that channel is frozen and projected forward for confirmation;
    this avoids letting the breakout candle redefine the line being tested.
    """
    frame = candles.sort_values("available_ms").reset_index(drop=True).copy()
    log_high = np.log(frame["high"].astype(float).to_numpy())
    log_close = np.log(frame["close"].astype(float).to_numpy())
    slope = np.full(len(frame), np.nan)
    upper = np.full(len(frame), np.nan)
    x = np.arange(lookback, dtype=float)
    for index in range(lookback, len(frame)):
        y = log_high[index - lookback : index]
        fitted_slope, intercept = np.polyfit(x, y, 1)
        fitted_sigma = float(np.std(y - (intercept + fitted_slope * x), ddof=0))
        slope[index] = fitted_slope
        upper[index] = intercept + fitted_slope * lookback + width_sigma * fitted_sigma
    frame["channel_slope_bps_per_bar"] = slope * 10_000
    frame["channel_upper"] = np.exp(upper)
    frame["upper_excess_bps"] = (log_close - upper) * 10_000
    frame["channel_break_probe"] = (slope < 0) & (log_close > upper)
    stable = np.zeros(len(frame), dtype=bool)
    stable_excess = np.full(len(frame), np.nan)
    stable_source_slope = np.full(len(frame), np.nan)
    for index in np.flatnonzero(frame["channel_break_probe"].to_numpy()):
        confirmation_index = index + stable_closes - 1
        if confirmation_index >= len(frame):
            continue
        projected = upper[index] + slope[index] * np.arange(stable_closes)
        if np.all(log_close[index : confirmation_index + 1] > projected):
            stable[confirmation_index] = True
            stable_excess[confirmation_index] = (
                log_close[confirmation_index] - projected[-1]
            ) * 10_000
            stable_source_slope[confirmation_index] = slope[index] * 10_000
    frame["stable_breakout_probe"] = stable
    frame["stable_upper_excess_bps"] = stable_excess
    frame["stable_source_slope_bps_per_bar"] = stable_source_slope
    return frame


def _latest_completed(frame: pd.DataFrame, timestamp_ms: int) -> pd.Series | None:
    eligible = frame[frame["available_ms"] <= timestamp_ms]
    return None if eligible.empty else eligible.iloc[-1]


def _quantiles(values: Iterable[float]) -> dict[str, float | None]:
    series = pd.Series(list(values), dtype=float).dropna()
    if series.empty:
        return {key: None for key in ("p10", "p25", "p50", "p75", "p90")}
    return {
        key: round(float(series.quantile(q)), 8)
        for key, q in (("p10", 0.1), ("p25", 0.25), ("p50", 0.5), ("p75", 0.75), ("p90", 0.9))
    }


def _campaign_anchors(audit_path: Path, fills_path: Path, orders_path: Path) -> pd.DataFrame:
    audit = pd.read_parquet(audit_path)
    plans = audit[audit["event_type"] == "entry_plan_created"].copy()
    plans["origin_price"] = plans["details"].map(
        lambda raw: float(json.loads(raw)["origin_price"])
    )
    plans = plans[["campaign_id", "event_time", "origin_price"]].rename(
        columns={"event_time": "signal_time_ms"}
    )
    first_fills = (
        audit[audit["event_type"] == "campaign_first_fill"]
        .groupby("campaign_id", as_index=False)["event_time"]
        .min()
        .rename(columns={"event_time": "first_fill_ms"})
    )
    anchors = plans.merge(first_fills, on="campaign_id", how="inner")

    fills = pd.read_parquet(fills_path)
    orders = pd.read_parquet(orders_path)[["order_id", "client_order_id", "side"]]
    entries = fills.merge(orders, on="order_id", how="inner", suffixes=("", "_order"))
    entries = entries[(entries["side"] == "SELL") & (entries["side_order"] == "SELL")].copy()
    entries["signal_time_ms"] = pd.to_numeric(
        entries["client_order_id"].str.extract(r"_(\d+)_tier\d+$")[0], errors="coerce"
    )
    entries["notional"] = entries["price"] * entries["quantity"]
    weighted = entries.groupby("signal_time_ms", as_index=False).agg(
        entry_notional=("notional", "sum"), entry_quantity=("quantity", "sum")
    )
    weighted["entry_price"] = weighted["entry_notional"] / weighted["entry_quantity"]
    return anchors.merge(
        weighted[["signal_time_ms", "entry_price"]],
        on="signal_time_ms",
        how="left",
    ).sort_values("first_fill_ms")


def _load_candles(
    connection: duckdb.DuckDBPyConnection,
    config: CalibrationConfig,
    timeframe: str,
    start_ms: int,
) -> pd.DataFrame:
    return connection.execute(
        """
        SELECT epoch_ms(open_time) AS open_ms,
               epoch_ms(close_time) AS available_ms,
               open, high, low, close, volume
          FROM candles
         WHERE symbol = ? AND timeframe = ?
           AND open_time >= to_timestamp(? / 1000.0)
           AND open_time < to_timestamp(? / 1000.0)
         ORDER BY open_time
        """,
        [config.symbol, timeframe, start_ms, config.study_end_ms],
    ).fetchdf()


def _first_origin_touch(
    connection: duckdb.DuckDBPyConnection,
    config: CalibrationConfig,
    first_fill_ms: int,
    origin_price: float,
) -> int | None:
    shift_ms = config.bar1s_time_shift_ms
    row = connection.execute(
        """
        SELECT min(epoch_ms(close_time))
          FROM candles
         WHERE symbol = ? AND timeframe = '1s'
           AND close_time >= to_timestamp(? / 1000.0)
           AND close_time < to_timestamp(? / 1000.0)
           AND low <= ?
        """,
        [
            config.symbol,
            first_fill_ms - shift_ms,
            config.study_end_ms - shift_ms,
            origin_price,
        ],
    ).fetchone()
    return None if row is None or row[0] is None else int(row[0]) + shift_ms


def _coverage(
    connection: duckdb.DuckDBPyConnection, config: CalibrationConfig
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    expected = {"1m": 60_000, "5m": 300_000, "15m": 900_000}
    for timeframe in ("1s", "1m", "5m", "15m"):
        shift_ms = config.bar1s_time_shift_ms if timeframe == "1s" else 0
        count, first_ms, last_ms = connection.execute(
            """
            SELECT count(*), min(epoch_ms(open_time)), max(epoch_ms(close_time))
              FROM candles
             WHERE symbol = ? AND timeframe = ?
               AND open_time >= to_timestamp(? / 1000.0)
               AND open_time < to_timestamp(? / 1000.0)
            """,
            [
                config.symbol,
                timeframe,
                config.study_start_ms - shift_ms,
                config.study_end_ms - shift_ms,
            ],
        ).fetchone()
        item: dict[str, Any] = {
            "rows": int(count),
            "first_open_ms": (
                int(first_ms) + shift_ms if first_ms is not None else None
            ),
            "last_close_ms": (
                int(last_ms) + shift_ms if last_ms is not None else None
            ),
        }
        if timeframe in expected:
            expected_rows = (config.study_end_ms - config.study_start_ms) // expected[timeframe]
            item["expected_rows"] = int(expected_rows)
            item["complete"] = count == expected_rows
        else:
            item["complete"] = None
            item["note"] = "1s archive contains trade-active bars, so density is not expected"
        result[timeframe] = item
    return result


def calibrate(
    *,
    duckdb_path: Path,
    audit_path: Path,
    fills_path: Path,
    orders_path: Path,
    config: CalibrationConfig,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    anchors = _campaign_anchors(audit_path, fills_path, orders_path)
    anchors = anchors[
        (anchors["first_fill_ms"] >= config.study_start_ms)
        & (anchors["first_fill_ms"] < config.study_end_ms)
    ].copy()
    if anchors.empty:
        raise ValueError("no filled campaigns in the study period")

    connection = duckdb.connect(str(duckdb_path), read_only=True)
    try:
        coverage = _coverage(connection, config)
        incomplete = [
            timeframe
            for timeframe in ("1m", "5m", "15m")
            if not coverage[timeframe]["complete"]
        ]
        if incomplete:
            raise ValueError(f"incomplete required candle coverage: {', '.join(incomplete)}")
        warmup_start = config.study_start_ms - 3 * 60 * 60 * 1000
        minute = momentum_indicators(
            _load_candles(connection, config, "1m", warmup_start), config
        )
        five = channel_breakout_candidates(
            _load_candles(connection, config, "5m", warmup_start),
            lookback=config.channel_5m_bars,
            width_sigma=config.channel_width_sigma,
            stable_closes=config.stable_closes,
        )
        fifteen = channel_breakout_candidates(
            _load_candles(connection, config, "15m", warmup_start),
            lookback=config.channel_15m_bars,
            width_sigma=config.channel_width_sigma,
            stable_closes=config.stable_closes,
        )

        records: list[dict[str, Any]] = []
        snapshots: list[dict[str, Any]] = []
        feature_columns = [
            "fast_log_slope_z",
            "slow_log_slope_z",
            "down_speed_ratio",
            "macd_hist_bps",
            "macd_hist_change_bps",
            "adx",
            "plus_di",
            "minus_di",
            "adx_change",
            "minus_di_change",
            "slope_decay_probe",
            "macd_recovery_probe",
            "adx_di_decay_probe",
            "decay_probe_agreement",
        ]
        for anchor in anchors.itertuples(index=False):
            touch = _first_origin_touch(
                connection, config, int(anchor.first_fill_ms), float(anchor.origin_price)
            )
            base: dict[str, Any] = {
                "campaign_id": anchor.campaign_id,
                "signal_time_ms": int(anchor.signal_time_ms),
                "first_fill_ms": int(anchor.first_fill_ms),
                "entry_price": float(anchor.entry_price),
                "origin_price": float(anchor.origin_price),
                "origin_reached": touch is not None,
                "origin_touch_ms": touch,
                "minutes_to_origin": (
                    (touch - int(anchor.first_fill_ms)) / 60_000 if touch is not None else None
                ),
                "right_censored": touch is None,
            }
            review_24h_ms = int(anchor.first_fill_ms) + 24 * 60 * 60 * 1_000
            base["review_24h_observed"] = review_24h_ms < config.study_end_ms
            if base["review_24h_observed"]:
                for label, frame in (("5m", five), ("15m", fifteen)):
                    review_row = _latest_completed(frame, review_24h_ms)
                    in_down_channel = bool(
                        review_row is not None
                        and review_row["channel_slope_bps_per_bar"] < 0
                        and review_row["close"] <= review_row["channel_upper"]
                    )
                    base[f"{label}_24h_down_channel_probe"] = in_down_channel
                base["both_24h_down_channel_probe"] = bool(
                    base["5m_24h_down_channel_probe"]
                    and base["15m_24h_down_channel_probe"]
                )
            for elapsed_seconds in config.entry_snapshot_seconds:
                timestamp = int(anchor.first_fill_ms) + elapsed_seconds * 1_000
                if timestamp >= config.study_end_ms:
                    continue
                row = _latest_completed(minute, timestamp)
                if row is None:
                    continue
                snapshot = {
                    "campaign_id": anchor.campaign_id,
                    "anchor": "first_fill",
                    "offset_seconds": elapsed_seconds,
                    "snapshot_ms": timestamp,
                    "close": float(row["close"]),
                }
                snapshot.update({column: row[column] for column in feature_columns})
                snapshots.append(snapshot)
            if touch is not None:
                origin_row = _latest_completed(minute, touch)
                if origin_row is not None:
                    for column in feature_columns:
                        base[f"origin_{column}"] = origin_row[column]
                for offset in config.snapshot_minutes:
                    timestamp = touch + offset * 60_000
                    if timestamp >= config.study_end_ms:
                        continue
                    row = _latest_completed(minute, timestamp)
                    if row is None:
                        continue
                    snapshot = {
                        "campaign_id": anchor.campaign_id,
                        "anchor": "origin",
                        "offset_seconds": offset * 60,
                        "snapshot_ms": timestamp,
                        "close": float(row["close"]),
                    }
                    snapshot.update({column: row[column] for column in feature_columns})
                    snapshots.append(snapshot)

                for label, frame in (("5m", five), ("15m", fifteen)):
                    matches = frame[
                        (frame["available_ms"] >= touch)
                        & (frame["available_ms"] < config.study_end_ms)
                        & frame["stable_breakout_probe"]
                    ]
                    first = None if matches.empty else matches.iloc[0]
                    base[f"{label}_stable_breakout_ms"] = (
                        None if first is None else int(first["available_ms"])
                    )
                    base[f"{label}_minutes_origin_to_breakout"] = (
                        None
                        if first is None
                        else (int(first["available_ms"]) - touch) / 60_000
                    )
                    base[f"{label}_breakout_excess_bps"] = (
                        None if first is None else float(first["stable_upper_excess_bps"])
                    )
            records.append(base)
    finally:
        connection.close()

    campaigns = pd.DataFrame(records).sort_values("first_fill_ms").reset_index(drop=True)
    snapshot_frame = pd.DataFrame(snapshots)
    reached = campaigns[campaigns["origin_reached"]]
    origin_metrics = {}
    for column in (
        "origin_fast_log_slope_z",
        "origin_slow_log_slope_z",
        "origin_down_speed_ratio",
        "origin_macd_hist_bps",
        "origin_macd_hist_change_bps",
        "origin_adx",
        "origin_plus_di",
        "origin_minus_di",
    ):
        origin_metrics[column.removeprefix("origin_")] = _quantiles(
            reached[column] if column in reached else []
        )
    agreement_counts: dict[str, int] = {}
    if "origin_decay_probe_agreement" in reached:
        agreement_counts = {
            str(int(key)): int(value)
            for key, value in reached["origin_decay_probe_agreement"]
            .value_counts()
            .sort_index()
            .items()
        }
    metric_samples = {
        column.removeprefix("origin_"): int(reached[column].notna().sum())
        for column in (
            "origin_fast_log_slope_z",
            "origin_slow_log_slope_z",
            "origin_down_speed_ratio",
            "origin_macd_hist_bps",
            "origin_macd_hist_change_bps",
            "origin_adx",
            "origin_plus_di",
            "origin_minus_di",
        )
    }
    entry_momentum: dict[str, Any] = {}
    if not snapshot_frame.empty:
        entry_rows = snapshot_frame[snapshot_frame["anchor"] == "first_fill"]
        for elapsed_seconds, group in entry_rows.groupby("offset_seconds"):
            entry_momentum[str(int(elapsed_seconds))] = {
                "samples": int(len(group)),
                "fast_log_slope_z": _quantiles(group["fast_log_slope_z"]),
                "slow_log_slope_z": _quantiles(group["slow_log_slope_z"]),
                "macd_hist_change_bps": _quantiles(group["macd_hist_change_bps"]),
                "adx": _quantiles(group["adx"]),
                "agreement_at_least_2": int((group["decay_probe_agreement"] >= 2).sum()),
            }
    summary = {
        "research_only": True,
        "production_parameters_frozen": False,
        "decision_scope": ["D-016", "D-017", "D-018", "D-019", "D-020", "D-025", "D-026"],
        "config": asdict(config),
        "coverage": coverage,
        "campaigns": {
            "filled": int(len(campaigns)),
            "origin_reached": int(campaigns["origin_reached"].sum()),
            "origin_not_reached_right_censored": int((~campaigns["origin_reached"]).sum()),
            "minutes_to_origin": _quantiles(campaigns["minutes_to_origin"]),
        },
        "origin_momentum_quantiles": origin_metrics,
        "origin_momentum_samples": metric_samples,
        "origin_decay_probe_agreement_counts": agreement_counts,
        "entry_elapsed_momentum": entry_momentum,
        "trend_breakout_candidates": {
            label: {
                "campaigns_with_candidate": int(campaigns[f"{label}_stable_breakout_ms"].notna().sum()),
                "minutes_after_origin": _quantiles(
                    campaigns[f"{label}_minutes_origin_to_breakout"]
                ),
                "upper_excess_bps": _quantiles(campaigns[f"{label}_breakout_excess_bps"]),
            }
            for label in ("5m", "15m")
        },
        "review_24h_candidates": {
            "observed": int(campaigns["review_24h_observed"].sum()),
            "right_censored_at_month_end": int((~campaigns["review_24h_observed"]).sum()),
            "5m_down_channel": int(
                campaigns.get("5m_24h_down_channel_probe", pd.Series(dtype=bool))
                .fillna(False)
                .sum()
            ),
            "15m_down_channel": int(
                campaigns.get("15m_24h_down_channel_probe", pd.Series(dtype=bool))
                .fillna(False)
                .sum()
            ),
            "both_timeframes_down_channel": int(
                campaigns.get("both_24h_down_channel_probe", pd.Series(dtype=bool))
                .fillna(False)
                .sum()
            ),
        },
    }
    return campaigns, snapshot_frame, summary


def _markdown_report(
    campaigns: pd.DataFrame, snapshots: pd.DataFrame, summary: dict[str, Any]
) -> str:
    config = summary["config"]
    reached = campaigns[campaigns["origin_reached"]].copy()
    lines = [
        "# AKEUSDT 2026 年 7 月退出策略标定（研究候选）",
        "",
        "> 本报告只用于 D-016 至 D-020、D-025、D-026 的离线研究。所有窗口、阈值和探针均为候选，**不是已确认生产参数**，也未接入 replay/testnet/live 执行策略。",
        "",
        "## 数据与口径",
        "",
        f"- 研究区间：`{_utc_text(config['study_start_ms'])}` 至 `{_utc_text(config['study_end_ms'])}`（右开，严格限定 2026 年 7 月）",
        f"- 样本：旧脚本现实执行 replay 中 {summary['campaigns']['filled']} 个实际发生入场成交的 Campaign；每个 Campaign 独立观察，重叠不去重",
        "- 旧 replay 的简单止盈/止损/超时只用于取得真实入场锚点，不截断后续观察；本报告按假设仍持仓的反事实路径观察至 origin、候选突破或月末",
        "- origin：第一笔入场成交后，1s Bar 的 `low <= origin_price` 首次成立；未在 7 月结束前触达的样本按右删失处理",
        "- 指标：只使用信号时点前或观察时点前已经完成的 K 线，不联网、不补数据、不回写 DuckDB",
        f"- 历史 1s 时间戳显式修正：`{config['bar1s_time_shift_ms']}` ms；该值必须来自数据对账证据，不自动推断",
        "- 1s 归档为有成交秒 Bar，不要求每秒稠密；1m/5m/15m 必须完整，否则工具拒绝运行",
        "",
        "复现命令：",
        "",
        "```bash",
        "PYTHONPATH=src python scripts/calibrate_spike_exits.py \\",
        "  --duckdb-path /data/projects/quant/crypto/data/market/history.duckdb \\",
        "  --replay-report reports/akeusdt_2026_07_legacy_replay_aligned \\",
        "  --bar1s-time-shift-hours 8 \\",
        "  --output reports/akeusdt_2026_07_exit_calibration_aligned \\",
        "  --report-file docs/research/AKEUSDT_2026_07_EXIT_CALIBRATION_ALIGNED.md",
        "```",
        "",
        "## 候选定义（未冻结）",
        "",
        f"- 快/慢下跌速度：1m log(close) 的 {config['fast_slope_bars']} / {config['slow_slope_bars']} 根 OLS slope，除以最近 {config['volatility_bars']} 根 log return 波动率；两者都为负时计算 `abs(fast)/abs(slow)`",
        f"- MACD：1m log(close) 的 {config['macd_fast']}/{config['macd_slow']}/{config['macd_signal']} histogram，以 bps 表示；同时观察 {config['momentum_change_bars']} 根变化",
        f"- ADX/DI：Wilder EWM {config['adx_bars']} 根，观察 ADX、+DI、-DI 及 {config['momentum_change_bars']} 根变化",
        "- 仅作对照的衰减探针：速度比 `<= 0.5`、MACD histogram 5 根变化 `> 0`、且下降方向占优时 ADX 与 -DI 同时走低；报告只统计三者同意数，不据此建议交易",
        f"- 趋势突破候选：每根 5m/15m K 只用之前 {config['channel_5m_bars']}/{config['channel_15m_bars']} 根 high 的 log 线性回归；负斜率回归线上轨加 {config['channel_width_sigma']} 倍残差标准差，首次突破时冻结并外推该通道，连续 {config['stable_closes']} 根完成 K 收在该上轨之上视为候选“站稳”",
        "",
        "## 数据覆盖",
        "",
        "| 周期 | 行数 | 期望行数 | 完整 |",
        "|---|---:|---:|---|",
    ]
    for timeframe, item in summary["coverage"].items():
        expected = item.get("expected_rows", "不适用")
        complete = "不适用（成交秒）" if item["complete"] is None else ("是" if item["complete"] else "否")
        lines.append(f"| {timeframe} | {item['rows']} | {expected} | {complete} |")
    campaigns_summary = summary["campaigns"]
    lines.extend(
        [
            "",
            "## Origin 触达",
            "",
            f"- 已触达：{campaigns_summary['origin_reached']} / {campaigns_summary['filled']} 个",
            f"- 截至月末未触达（右删失）：{campaigns_summary['origin_not_reached_right_censored']} 个",
            f"- 入场至触达分钟分位数：`{json.dumps(campaigns_summary['minutes_to_origin'], ensure_ascii=False)}`",
            "",
            "## Origin 时点动能分布",
            "",
            "以下均是观测分布，不能直接转换为生产阈值。",
            "",
            "| 指标 | n | p10 | p25 | p50 | p75 | p90 |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for metric, quantiles in summary["origin_momentum_quantiles"].items():
        lines.append(
            f"| {metric} | {summary['origin_momentum_samples'][metric]} | {quantiles['p10']} | {quantiles['p25']} | {quantiles['p50']} | {quantiles['p75']} | {quantiles['p90']} |"
        )
    lines.extend(
        [
            "",
            f"三个衰减探针在 origin 时点的同意数分布：`{json.dumps(summary['origin_decay_probe_agreement_counts'], ensure_ascii=False)}`。",
            "",
            "## 第一笔成交后的时间截面（D-018）",
            "",
            "这里只观察 90 秒及以后的动能分布，不设置固定价格止损，也不猜测时间风险/动能风险的收严函数。",
            "",
            "| 已持仓时间 | n | 快 slope z p50 | 慢 slope z p50 | MACD 变化 p50 | ADX p50 | 探针同意数 >= 2 |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for elapsed, item in summary["entry_elapsed_momentum"].items():
        lines.append(
            f"| {int(elapsed) / 60:g} 分钟 | {item['samples']} | {item['fast_log_slope_z']['p50']} | {item['slow_log_slope_z']['p50']} | {item['macd_hist_change_bps']['p50']} | {item['adx']['p50']} | {item['agreement_at_least_2']} |"
        )
    lines.extend(
        [
            "",
            "## 5m / 15m 趋势突破候选",
            "",
            "| 周期 | 出现候选的 Campaign | origin 后分钟分位数 | 突破上轨 bps 分位数 |",
            "|---|---:|---|---|",
        ]
    )
    for label, item in summary["trend_breakout_candidates"].items():
        lines.append(
            f"| {label} | {item['campaigns_with_candidate']} | `{json.dumps(item['minutes_after_origin'], ensure_ascii=False)}` | `{json.dumps(item['upper_excess_bps'], ensure_ascii=False)}` |"
        )
    review = summary["review_24h_candidates"]
    lines.extend(
        [
            "",
            "## 24 小时风险复核候选",
            "",
            f"月末前能观察到完整 24 小时的 Campaign 为 {review['observed']} 个，另有 {review['right_censored_at_month_end']} 个右删失。沿用同一候选回归通道，在复核时点满足负斜率且收盘不高于上轨的样本：5m 为 {review['5m_down_channel']} 个、15m 为 {review['15m_down_channel']} 个、两周期同时满足为 {review['both_timeframes_down_channel']} 个。该状态只是 D-020 算法标定输入，不代表获准继续持仓。",
        ]
    )
    lines.extend(["", "## 案例", ""])
    case_columns = [
        "campaign_id",
        "first_fill_ms",
        "origin_touch_ms",
        "minutes_to_origin",
        "origin_down_speed_ratio",
        "origin_macd_hist_change_bps",
        "origin_adx",
        "origin_minus_di",
        "origin_decay_probe_agreement",
        "5m_minutes_origin_to_breakout",
        "15m_minutes_origin_to_breakout",
    ]
    cases = reached.sort_values("minutes_to_origin").head(8)
    lines.extend(
        [
            "| Campaign | 首次成交 UTC | origin UTC | 分钟 | 速度比 | MACD 变化 bps | ADX | -DI | 探针同意数 | 5m 突破分钟 | 15m 突破分钟 |",
            "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in cases[case_columns].itertuples(index=False, name=None):
        campaign, fill_ms, touch_ms, minutes, ratio, macd_change, adx, minus_di, agreement, five, fifteen = row
        values = [ratio, macd_change, adx, minus_di, agreement, five, fifteen]
        formatted = ["" if pd.isna(value) else f"{float(value):.4f}" for value in values]
        lines.append(
            f"| {campaign} | {_utc_text(fill_ms)} | {_utc_text(touch_ms)} | {minutes:.4f} | "
            + " | ".join(formatted)
            + " |"
        )
    lines.extend(
        [
            "",
            "CLI 输出目录中的 `campaigns.csv` 是完整逐 Campaign 结果；`momentum_snapshots.csv` 包含第一笔成交后 90/300/900/3600 秒及 origin 后 0/5/15/30/60 分钟动能快照；`summary.json` 是机器可读汇总。",
            "",
            "## 解释限制与下一步",
            "",
            f"- 样本仅为单币种单月且以发生实际入场的 {summary['campaigns']['filled']} 个 Campaign 为条件，不能据此冻结通用阈值。",
            "- 本轮只描述信号分布，未将减半/清仓动作施加到资金曲线，避免用候选规则反向选择最好收益。",
            "- D-018 的 90 秒后时间风险与动能风险收严曲线仍是 candidate，本工具不把单月最优值伪装成生产结论。",
            "- 下一轮增加多币种、多市场阶段样本并做 walk-forward；指标窗口、探针组合、通道长度和“站稳”根数按 D-027 用 replay/testnet 证据迭代，不要求用户凭经验直接给值。",
            "",
        ]
    )
    return "\n".join(lines)


def write_outputs(
    output_dir: Path,
    campaigns: pd.DataFrame,
    snapshots: pd.DataFrame,
    summary: dict[str, Any],
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    campaigns.to_csv(output_dir / "campaigns.csv", index=False, float_format="%.10g")
    snapshots.to_csv(
        output_dir / "momentum_snapshots.csv", index=False, float_format="%.10g"
    )
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (output_dir / "REPORT.md").write_text(
        _markdown_report(campaigns, snapshots, summary), encoding="utf-8"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Calibrate unconfirmed spike exit candidates")
    parser.add_argument("--duckdb-path", type=Path, required=True)
    parser.add_argument("--replay-report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--bar1s-time-shift-hours",
        type=float,
        default=0.0,
        help="Explicit historical 1s timestamp correction; default 0",
    )
    parser.add_argument(
        "--report-file",
        type=Path,
        default=None,
        help="Optional tracked Markdown report path",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = CalibrationConfig(
        bar1s_time_shift_ms=int(args.bar1s_time_shift_hours * 3_600_000)
    )
    campaigns, snapshots, summary = calibrate(
        duckdb_path=args.duckdb_path,
        audit_path=args.replay_report / "audit_events.parquet",
        fills_path=args.replay_report / "fills.parquet",
        orders_path=args.replay_report / "orders.parquet",
        config=config,
    )
    write_outputs(args.output, campaigns, snapshots, summary)
    if args.report_file is not None:
        args.report_file.parent.mkdir(parents=True, exist_ok=True)
        args.report_file.write_text(
            _markdown_report(campaigns, snapshots, summary), encoding="utf-8"
        )
    print(f"Research report written to {args.output}")


if __name__ == "__main__":
    main()
