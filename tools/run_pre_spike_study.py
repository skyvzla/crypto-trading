"""W4: 修正研究协议后的 pre-spike 前兆因子实验。

核心纪律：
1. 只通过 archive index 选择 1m Parquet，不直接 glob，避免月/日混合分区。
2. 条件概率和 base rate 使用完全相同的 future-spike 标签。
3. 数据末端不足完整 horizon 的行按 right-censored 排除，不当作负样本。
4. train 只负责选择一条预先声明的数值规则；test 不重算分位、不调阈值。
5. 密集前兆按 cooldown 合成 alert episode，避免连续多分钟重复计数。

默认时间：
    train: [2026-02-01, 2026-07-31 23:00) UTC
    embargo: 1h
    test:  [2026-08-01, 2026-08-22) UTC

用法：
    python tools/run_pre_spike_study.py --workers 10
"""

from __future__ import annotations

import argparse
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from trading_platform.research.factor_lab.pre_event import (
    PreSpikeCondition,
    add_pre_spike_factors,
    condition_mask,
    cooldown_alert_mask,
    event_capture_stats,
    future_event_labels,
    recent_event_mask,
    wilson_interval,
)


DEFAULT_ARCHIVE = Path("data/market/candles")
DEFAULT_INDEX = Path("data/market/candles/archive_index.parquet")
DEFAULT_OUT = Path("docs/research/PRE_SPIKE_FACTOR_STUDY_W4.md")

DEFAULT_TRAIN_START = "2026-02-01T00:00:00+00:00"
DEFAULT_TRAIN_END = "2026-07-31T23:00:00+00:00"
DEFAULT_TEST_START = "2026-08-01T00:00:00+00:00"
DEFAULT_TEST_END = "2026-08-22T00:00:00+00:00"

HORIZONS = (5, 10, 20)
PRIMARY_HORIZON = 5
ALERT_COOLDOWN_BARS = 5
SPIKE_EVENT_COOLDOWN_BARS = 5
QUIET_LOOKBACK_BARS = 20
MIN_TRAIN_ALERTS = 100


def _conditions() -> tuple[PreSpikeCondition, ...]:
    conditions: list[PreSpikeCondition] = []
    single_grids = {
        "atr_mult": (2.0, 3.0, 5.0),
        "wick_pct": (1.0, 2.0, 4.0),
        "bbw_mult": (2.0, 3.0, 5.0),
        "volume_mult": (2.0, 3.0, 5.0),
        "return_5m": (0.02, 0.05, 0.10),
    }
    for factor, thresholds in single_grids.items():
        for threshold in thresholds:
            conditions.append(
                PreSpikeCondition(
                    name=factor,
                    thresholds=((factor, threshold),),
                )
            )
    for atr in (2.0, 3.0, 5.0):
        for wick in (1.0, 2.0, 4.0):
            conditions.append(
                PreSpikeCondition(
                    name="atr_wick",
                    thresholds=(("atr_mult", atr), ("wick_pct", wick)),
                )
            )
    for atr in (2.0, 3.0):
        for wick in (1.0, 2.0):
            for bbw in (2.0, 3.0):
                conditions.append(
                    PreSpikeCondition(
                        name="atr_wick_bbw",
                        thresholds=(
                            ("atr_mult", atr),
                            ("wick_pct", wick),
                            ("bbw_mult", bbw),
                        ),
                    )
                )
    return tuple(conditions)


CONDITIONS = _conditions()
CONDITION_BY_KEY = {condition.label: condition for condition in CONDITIONS}


@dataclass
class SymbolStudyContext:
    symbol: str
    factors: pd.DataFrame
    timestamps: np.ndarray
    segment_ids: np.ndarray
    valid: np.ndarray
    warning_eligible: np.ndarray
    spike_events: np.ndarray
    future: dict[int, tuple[np.ndarray, np.ndarray]]
    segments: dict[str, np.ndarray]


def _timestamp_ms(value: str) -> int:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamps must include timezone")
    return int(timestamp.timestamp() * 1_000)


def _discover_symbol_files(
    archive_root: Path,
    index_path: Path,
    *,
    start_ms: int,
    end_ms: int,
) -> dict[str, tuple[str, ...]]:
    index = pd.read_parquet(
        index_path,
        columns=[
            "symbol",
            "timeframe",
            "first_open_ms",
            "last_close_ms",
            "relative_path",
        ],
    )
    selected = index[
        index["timeframe"].eq("1m")
        & index["first_open_ms"].lt(end_ms)
        & index["last_close_ms"].ge(start_ms)
    ]
    result: dict[str, tuple[str, ...]] = {}
    for symbol, group in selected.groupby("symbol", sort=True):
        result[str(symbol).upper()] = tuple(
            str((archive_root / relative).resolve())
            for relative in group["relative_path"].drop_duplicates().astype(str)
        )
    return result


def _load_symbol_context(
    job: tuple[str, tuple[str, ...], int, int, int, int]
) -> SymbolStudyContext | None:
    symbol, source_files, train_start, train_end, test_start, test_end = job
    if not source_files:
        return None
    warmup_ms = 120 * 60_000
    lookahead_ms = max(HORIZONS) * 60_000
    start_ms = train_start - warmup_ms
    end_ms = test_end + lookahead_ms
    connection = duckdb.connect()
    try:
        bars = connection.execute(
            """
            SELECT epoch_ms(open_time)::BIGINT AS open_ms,
                   open, high, low, close, volume
            FROM read_parquet(?, union_by_name=true)
            WHERE symbol = ? AND timeframe = '1m'
              AND epoch_ms(open_time) >= ? AND epoch_ms(open_time) < ?
            ORDER BY open_time
            """,
            [list(source_files), symbol, start_ms, end_ms],
        ).fetch_df()
    finally:
        connection.close()
    if len(bars) < 200:
        return None

    factors = add_pre_spike_factors(bars)
    timestamps = factors["open_ms"].to_numpy(np.int64)
    required_factors = (
        "atr_mult",
        "bbw_mult",
        "wick_pct",
        "volume_mult",
        "return_5m",
    )
    valid = np.ones(len(factors), dtype=bool)
    for column in required_factors:
        valid &= np.isfinite(
            pd.to_numeric(factors[column], errors="coerce").to_numpy(float)
        )
    segment_ids = factors["segment_id"].to_numpy(np.int64)
    raw_spikes = factors["spike_15pct"].to_numpy(bool)
    spike_events = cooldown_alert_mask(
        raw_spikes,
        cooldown_bars=SPIKE_EVENT_COOLDOWN_BARS,
        segment_ids=segment_ids,
    )
    recent_spike = recent_event_mask(
        raw_spikes,
        lookback_bars=QUIET_LOOKBACK_BARS,
        segment_ids=segment_ids,
    )
    warning_eligible = valid & ~recent_spike
    future = future_event_labels(
        spike_events,
        HORIZONS,
        segment_ids=segment_ids,
    )
    segments = {
        "train": (timestamps >= train_start) & (timestamps < train_end),
        "test": (timestamps >= test_start) & (timestamps < test_end),
    }
    return SymbolStudyContext(
        symbol=symbol,
        factors=factors,
        timestamps=timestamps,
        segment_ids=segment_ids,
        valid=valid,
        warning_eligible=warning_eligible,
        spike_events=spike_events,
        future=future,
        segments=segments,
    )


def _process_symbol(
    job: tuple[str, tuple[str, ...], int, int, int, int]
) -> dict[str, object] | None:
    context = _load_symbol_context(job)
    if context is None:
        return None
    symbol = context.symbol
    factors = context.factors
    segment_ids = context.segment_ids
    valid = context.valid
    warning_eligible = context.warning_eligible
    spike_events = context.spike_events
    future = context.future
    segments = context.segments

    rows: list[dict[str, object]] = []
    for segment, segment_mask in segments.items():
        for horizon in HORIZONS:
            future_hit, eligible = future[horizon]
            base_mask = warning_eligible & segment_mask & eligible
            rows.append(
                {
                    "symbol": symbol,
                    "condition": "background",
                    "segment": segment,
                    "horizon": horizon,
                    "n": int(base_mask.sum()),
                    "hits": int((base_mask & future_hit).sum()),
                    "events": int((spike_events & segment_mask).sum()),
                    "captured_events": 0,
                    "lead_sum_bars": 0.0,
                }
            )

    for condition in CONDITIONS:
        raw_condition = warning_eligible & condition_mask(factors, condition)
        alerts = cooldown_alert_mask(
            raw_condition,
            cooldown_bars=ALERT_COOLDOWN_BARS,
            segment_ids=segment_ids,
        )
        for segment, segment_mask in segments.items():
            for horizon in HORIZONS:
                future_hit, eligible = future[horizon]
                mask = alerts & segment_mask & eligible
                capture = event_capture_stats(
                    alerts & segment_mask,
                    spike_events,
                    horizon_bars=horizon,
                    eligible_events=segment_mask,
                    segment_ids=segment_ids,
                )
                rows.append(
                    {
                        "symbol": symbol,
                        "condition": condition.label,
                        "segment": segment,
                        "horizon": horizon,
                        "n": int(mask.sum()),
                        "hits": int((mask & future_hit).sum()),
                        "events": int(capture["events"]),
                        "captured_events": int(capture["captured_events"]),
                        "lead_sum_bars": float(capture["lead_sum_bars"]),
                    }
                )
    return {
        "rows": rows,
        "bars": {
            segment: int((valid & segment_mask).sum())
            for segment, segment_mask in segments.items()
        },
    }


def _aggregate(rows: list[dict[str, object]]) -> pd.DataFrame:
    frame = pd.DataFrame(rows)
    aggregated = frame.groupby(
        ["condition", "segment", "horizon"], as_index=False
    ).agg(
        n=("n", "sum"),
        hits=("hits", "sum"),
        events=("events", "sum"),
        captured_events=("captured_events", "sum"),
        lead_sum_bars=("lead_sum_bars", "sum"),
    )
    aggregated["prob"] = aggregated["hits"].div(
        aggregated["n"].replace(0, np.nan)
    )
    base = aggregated[aggregated["condition"].eq("background")][
        ["segment", "horizon", "n", "hits", "prob"]
    ].rename(
        columns={
            "n": "base_n",
            "hits": "base_hits",
            "prob": "base_prob",
        }
    )
    result = aggregated.merge(base, on=["segment", "horizon"], how="left")
    result["lift"] = result["prob"].div(result["base_prob"].replace(0, np.nan))
    result["recall"] = result["captured_events"].div(
        result["events"].replace(0, np.nan)
    )
    result["mean_lead_minutes"] = result["lead_sum_bars"].div(
        result["captured_events"].replace(0, np.nan)
    )
    intervals = result.apply(
        lambda row: wilson_interval(int(row["hits"]), int(row["n"])),
        axis=1,
    )
    result["prob_ci_low"] = [value[0] for value in intervals]
    result["prob_ci_high"] = [value[1] for value in intervals]
    base_intervals = result.apply(
        lambda row: wilson_interval(int(row["base_hits"]), int(row["base_n"])),
        axis=1,
    )
    result["base_ci_low"] = [value[0] for value in base_intervals]
    result["base_ci_high"] = [value[1] for value in base_intervals]
    return result


def _select_condition(result: pd.DataFrame, min_train_alerts: int) -> str | None:
    candidates = result[
        result["segment"].eq("train")
        & result["horizon"].eq(PRIMARY_HORIZON)
        & ~result["condition"].eq("background")
        & result["n"].ge(min_train_alerts)
        & result["hits"].gt(0)
        & np.isfinite(result["lift"])
    ].sort_values(["lift", "n"], ascending=[False, False], kind="stable")
    return None if candidates.empty else str(candidates.iloc[0]["condition"])


def _process_selected_daily(
    job: tuple[str, tuple[str, ...], int, int, int, int, str]
) -> list[dict[str, object]]:
    symbol, source_files, train_start, train_end, test_start, test_end, selected = job
    context = _load_symbol_context(
        (symbol, source_files, train_start, train_end, test_start, test_end)
    )
    if context is None:
        return []
    condition = CONDITION_BY_KEY[selected]
    raw_condition = context.warning_eligible & condition_mask(
        context.factors, condition
    )
    alerts = cooldown_alert_mask(
        raw_condition,
        cooldown_bars=ALERT_COOLDOWN_BARS,
        segment_ids=context.segment_ids,
    )
    future_hit, eligible = context.future[PRIMARY_HORIZON]
    day_id = context.timestamps // (24 * 60 * 60_000)
    rows: list[dict[str, object]] = []
    for segment, segment_mask in context.segments.items():
        for day in np.unique(day_id[segment_mask]):
            day_mask = segment_mask & (day_id == day)
            base_mask = context.warning_eligible & day_mask & eligible
            alert_mask = alerts & day_mask & eligible
            rows.append(
                {
                    "segment": segment,
                    "day": int(day),
                    "alerts": int(alert_mask.sum()),
                    "hits": int((alert_mask & future_hit).sum()),
                    "base_n": int(base_mask.sum()),
                    "base_hits": int((base_mask & future_hit).sum()),
                }
            )
    return rows


def _block_bootstrap_by_day(
    daily_rows: list[dict[str, object]],
    *,
    segment: str,
    resamples: int = 2_000,
    seed: int = 20260822,
) -> dict[str, float | int]:
    frame = pd.DataFrame(daily_rows)
    if frame.empty:
        return {"days": 0}
    grouped = frame[frame["segment"].eq(segment)].groupby("day", as_index=False).agg(
        alerts=("alerts", "sum"),
        hits=("hits", "sum"),
        base_n=("base_n", "sum"),
        base_hits=("base_hits", "sum"),
    )
    if len(grouped) < 5:
        return {"days": int(len(grouped))}
    arrays = grouped[["alerts", "hits", "base_n", "base_hits"]].to_numpy(float)
    rng = np.random.default_rng(seed)
    precisions: list[float] = []
    base_probs: list[float] = []
    lifts: list[float] = []
    for _ in range(resamples):
        sampled = arrays[rng.integers(0, len(arrays), size=len(arrays))].sum(axis=0)
        alerts, hits, base_n, base_hits = sampled
        if alerts <= 0 or base_n <= 0 or base_hits <= 0:
            continue
        precision = hits / alerts
        base_prob = base_hits / base_n
        precisions.append(float(precision))
        base_probs.append(float(base_prob))
        lifts.append(float(precision / base_prob))
    if len(lifts) < max(100, resamples // 4):
        return {"days": int(len(grouped)), "valid_resamples": int(len(lifts))}

    def interval(values: list[float]) -> tuple[float, float]:
        low, high = np.quantile(np.asarray(values), [0.025, 0.975])
        return float(low), float(high)

    precision_low, precision_high = interval(precisions)
    base_low, base_high = interval(base_probs)
    lift_low, lift_high = interval(lifts)
    return {
        "days": int(len(grouped)),
        "valid_resamples": int(len(lifts)),
        "precision_ci_low": precision_low,
        "precision_ci_high": precision_high,
        "base_ci_low": base_low,
        "base_ci_high": base_high,
        "lift_ci_low": lift_low,
        "lift_ci_high": lift_high,
    }


def _condition_description(label: str) -> str:
    condition = CONDITION_BY_KEY.get(label)
    return label if condition is None else condition.label


def _format_probability(value: float) -> str:
    return "-" if not np.isfinite(value) else f"{value:.3%}"


def _format_minutes(value: float) -> str:
    return "-" if not np.isfinite(value) else f"{value:.2f}m"


def _render_report(
    result: pd.DataFrame,
    *,
    symbols: int,
    symbols_with_data: int,
    bars_by_segment: dict[str, int],
    train_start: int,
    train_end: int,
    test_start: int,
    test_end: int,
    elapsed_seconds: float,
    min_train_alerts: int,
    block_bootstrap: dict[str, dict[str, float | int]] | None = None,
) -> str:
    primary_train = result[
        result["segment"].eq("train")
        & result["horizon"].eq(PRIMARY_HORIZON)
        & ~result["condition"].eq("background")
        & result["n"].ge(min_train_alerts)
        & result["hits"].gt(0)
        & np.isfinite(result["lift"])
    ].sort_values(["lift", "n"], ascending=[False, False], kind="stable")
    selected = _select_condition(result, min_train_alerts)

    def ts(value: int) -> str:
        return pd.Timestamp(value, unit="ms", tz="UTC").isoformat()

    lines = [
        "# Pre-Spike 因子研究 W4：修正 base rate + 新时间外推",
        "",
        "> 这是一轮 WARNING/pre-event 研究，不是做空入场回测。测试段 2026-08 为前述 W1/W2 未使用的新 holdout。",
        "",
        "## 1. 实验协议",
        "",
        f"- 归档候选 symbol: {symbols}；实际有足够数据: {symbols_with_data}",
        f"- train: `{ts(train_start)}` ≤ t < `{ts(train_end)}`",
        f"- embargo: `{ts(train_end)}` → `{ts(test_start)}`",
        f"- test: `{ts(test_start)}` ≤ t < `{ts(test_end)}`",
        f"- 有效 1m bars: train={bars_by_segment.get('train', 0):,} / test={bars_by_segment.get('test', 0):,}",
        "- spike 标签：未来 N 根 1m 内出现 `high >= 前一根 close × 1.15`；**不含当前 bar**。",
        "- base rate：在完全相同的 future 标签、完整 horizon 和有效因子观测集合上计算。",
        f"- genuine pre-event 约束：当前及过去 {QUIET_LOOKBACK_BARS} 分钟不得已经出现 15% spike。",
        f"- alert episode cooldown: {ALERT_COOLDOWN_BARS} 根 1m；避免连续状态重复报样本。",
        f"- 规则只从预先声明的 {len(CONDITIONS)} 个数值阈值组合中选择；test 不 qcut、不重拟合阈值。",
        f"- train 最低触发数: {min_train_alerts}；主选择 horizon={PRIMARY_HORIZON}m。",
        f"- 运行耗时: {elapsed_seconds:.1f}s",
        "",
        "## 2. 无条件背景概率",
        "",
        "| Horizon | Train | Train n | Test | Test n |",
        "|---:|---:|---:|---:|---:|",
    ]
    for horizon in HORIZONS:
        train = result[
            result["condition"].eq("background")
            & result["segment"].eq("train")
            & result["horizon"].eq(horizon)
        ].iloc[0]
        test = result[
            result["condition"].eq("background")
            & result["segment"].eq("test")
            & result["horizon"].eq(horizon)
        ].iloc[0]
        lines.append(
            f"| {horizon}m | {_format_probability(float(train.prob))} | {int(train.n):,} | "
            f"{_format_probability(float(test.prob))} | {int(test.n):,} |"
        )

    lines += ["", "## 3. Train 因子筛选与固定规则", ""]
    if selected is None:
        lines.append("没有规则达到最低 train 样本要求，不能进入样本外验证。")
        return "\n".join(lines) + "\n"

    top_train = primary_train.head(12)[
        ["condition", "n", "prob", "base_prob", "lift", "recall", "mean_lead_minutes"]
    ]
    lines += [
        "| Rank | Rule | Alerts | P(spike≤5m) | Base | Lift | Event Recall | Mean Lead |",
        "|---:|---|---:|---:|---:|---:|---:|---:|",
    ]
    for rank, row in enumerate(top_train.itertuples(index=False), start=1):
        lines.append(
            f"| {rank} | `{row.condition}` | {int(row.n):,} | {_format_probability(float(row.prob))} | "
            f"{_format_probability(float(row.base_prob))} | {float(row.lift):.2f} | "
            f"{_format_probability(float(row.recall))} | {_format_minutes(float(row.mean_lead_minutes))} |"
        )
    lines += [
        "",
        f"**冻结后进入 test 的规则：** `{_condition_description(selected)}`",
        "",
        "## 4. 冻结规则的样本外表现",
        "",
        "| Horizon | Segment | Alerts | Precision | 95% Wilson CI | Base | Lift | Event Recall | Mean Lead |",
        "|---:|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    selected_rows = result[result["condition"].eq(selected)].sort_values(
        ["horizon", "segment"], kind="stable"
    )
    for horizon in HORIZONS:
        for segment in ("train", "test"):
            row = selected_rows[
                selected_rows["horizon"].eq(horizon)
                & selected_rows["segment"].eq(segment)
            ].iloc[0]
            ci = (
                f"[{_format_probability(float(row.prob_ci_low))}, "
                f"{_format_probability(float(row.prob_ci_high))}]"
            )
            lines.append(
                f"| {horizon}m | {segment} | {int(row.n):,} | {_format_probability(float(row.prob))} | "
                f"{ci} | {_format_probability(float(row.base_prob))} | {float(row.lift):.2f} | "
                f"{_format_probability(float(row.recall))} | {_format_minutes(float(row.mean_lead_minutes))} |"
            )

    if block_bootstrap:
        lines += [
            "",
            "### 4.1 按 UTC 日 block-bootstrap（主 5m horizon）",
            "",
            "同一天所有币的共同冲击作为一个 block；主结论优先看这里，而不是把每条 alert 当独立样本。",
            "",
            "| Segment | Days | Precision 95% CI | Base 95% CI | Lift 95% CI |",
            "|---|---:|---:|---:|---:|",
        ]
        for segment in ("train", "test"):
            stats = block_bootstrap.get(segment, {})
            precision_ci = (
                "-"
                if "precision_ci_low" not in stats
                else f"[{_format_probability(float(stats['precision_ci_low']))}, {_format_probability(float(stats['precision_ci_high']))}]"
            )
            base_ci = (
                "-"
                if "base_ci_low" not in stats
                else f"[{_format_probability(float(stats['base_ci_low']))}, {_format_probability(float(stats['base_ci_high']))}]"
            )
            lift_ci = (
                "-"
                if "lift_ci_low" not in stats
                else f"[{float(stats['lift_ci_low']):.2f}, {float(stats['lift_ci_high']):.2f}]"
            )
            lines.append(
                f"| {segment} | {int(stats.get('days', 0))} | {precision_ci} | {base_ci} | {lift_ci} |"
            )

    selected_condition = CONDITION_BY_KEY[selected]
    component_labels: list[str] = []
    for factor, threshold in selected_condition.thresholds:
        label = PreSpikeCondition(
            name=factor,
            thresholds=((factor, threshold),),
        ).label
        if label in CONDITION_BY_KEY:
            component_labels.append(label)
    if len(component_labels) >= 2:
        lines += [
            "",
            "### 4.2 组合相对单因子的增量",
            "",
            "| Segment | Rule | Alerts | Precision | Lift | Recall |",
            "|---|---|---:|---:|---:|---:|",
        ]
        comparison_labels = [*component_labels, selected]
        for segment in ("train", "test"):
            for label in comparison_labels:
                row = result[
                    result["condition"].eq(label)
                    & result["segment"].eq(segment)
                    & result["horizon"].eq(PRIMARY_HORIZON)
                ]
                if row.empty:
                    continue
                value = row.iloc[0]
                lines.append(
                    f"| {segment} | `{label}` | {int(value.n):,} | "
                    f"{_format_probability(float(value.prob))} | {float(value.lift):.2f} | "
                    f"{_format_probability(float(value.recall))} |"
                )

    test_primary = selected_rows[
        selected_rows["segment"].eq("test")
        & selected_rows["horizon"].eq(PRIMARY_HORIZON)
    ].iloc[0]
    train_primary = selected_rows[
        selected_rows["segment"].eq("train")
        & selected_rows["horizon"].eq(PRIMARY_HORIZON)
    ].iloc[0]
    lines += [
        "",
        "## 5. 结论",
        "",
        f"- train 5m lift = **{float(train_primary.lift):.2f}**；test 5m lift = **{float(test_primary.lift):.2f}**。",
        f"- test 触发 {int(test_primary.n):,} 次，命中率 {_format_probability(float(test_primary.prob))}，背景 {_format_probability(float(test_primary.base_prob))}。",
        f"- test 事件级 recall = {_format_probability(float(test_primary.recall))}，平均提前 {_format_minutes(float(test_primary.mean_lead_minutes))}。",
    ]
    if len(component_labels) >= 2:
        component_test = result[
            result["condition"].isin(component_labels)
            & result["segment"].eq("test")
            & result["horizon"].eq(PRIMARY_HORIZON)
        ]
        best_component_precision = component_test["prob"].max()
        if np.isfinite(best_component_precision) and best_component_precision > 0:
            incremental = float(test_primary.prob) / float(best_component_precision)
            lines.append(
                f"- 组合相对最强单因子 test precision 增量 = **{incremental:.2f}×**。"
            )
    if int(test_primary.n) < 30:
        lines.append("- test 触发数过少：统计功效不足，本轮只能记录方向，不能进入策略。")
    elif float(test_primary.prob) < 0.02 or float(test_primary.recall) < 0.10:
        lines.append(
            "- 虽然 enrichment 明显，但 precision/recall 都偏低：当前只能作为极窄的候选池 gate，不能称为完整 WARNING，更不能触发挂单。"
        )
    elif float(test_primary.lift) > 1.0:
        lines.append("- 点估计在新 holdout 上仍高于背景；可继续作为 WARNING 候选验证，但还不是开空信号。")
    else:
        lines.append("- 新 holdout 未优于背景；该前兆规则不应进入策略。")
    lines += [
        "",
        "## 6. 解释边界与下一步",
        "",
        "1. 主结论优先看按日 block-bootstrap；Wilson CI 仅保留用于直观展示单条 alert 的二项区间。",
        "2. 当前 target 是 1m 内 15% 极端上冲，和 1s spike 触发并非完全同一个标签；本轮回答的是 WARNING/universe 预热价值。",
        "3. 本轮不计算 MFE/MAE ‘expectancy’，因为 pre-event 信号不是做空入场，且极值没有触达顺序。",
        "4. 若规则样本外有效，下一轮应加入 OI/多空比慢因子和 BTC-relative 因子，检验增量 lift，而不是直接加到 scoring。",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, default=DEFAULT_ARCHIVE)
    parser.add_argument("--index", type=Path, default=DEFAULT_INDEX)
    parser.add_argument("--workers", type=int, default=10)
    parser.add_argument(
        "--max-symbols",
        type=int,
        default=None,
        help="optional deterministic symbol cap for smoke tests; omit for the real study",
    )
    parser.add_argument(
        "--train-start", type=_timestamp_ms, default=_timestamp_ms(DEFAULT_TRAIN_START)
    )
    parser.add_argument(
        "--train-end", type=_timestamp_ms, default=_timestamp_ms(DEFAULT_TRAIN_END)
    )
    parser.add_argument(
        "--test-start", type=_timestamp_ms, default=_timestamp_ms(DEFAULT_TEST_START)
    )
    parser.add_argument(
        "--test-end", type=_timestamp_ms, default=_timestamp_ms(DEFAULT_TEST_END)
    )
    parser.add_argument("--min-train-alerts", type=int, default=MIN_TRAIN_ALERTS)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)
    if args.workers <= 0:
        parser.error("--workers must be positive")
    if args.max_symbols is not None and args.max_symbols <= 0:
        parser.error("--max-symbols must be positive")
    if not (args.train_start < args.train_end <= args.test_start < args.test_end):
        parser.error("require train_start < train_end <= test_start < test_end")
    if args.test_start - args.train_end < 60 * 60_000:
        parser.error("train/test embargo must be at least 1 hour")
    if args.min_train_alerts <= 0:
        parser.error("--min-train-alerts must be positive")
    if not args.archive.is_dir():
        parser.error(f"archive root not found: {args.archive}")
    if not args.index.is_file():
        parser.error(f"archive index not found: {args.index}")

    started = time.monotonic()
    archive_start_ms = args.train_start - 120 * 60_000
    archive_end_ms = args.test_end + max(HORIZONS) * 60_000
    source_files = _discover_symbol_files(
        args.archive,
        args.index,
        start_ms=archive_start_ms,
        end_ms=archive_end_ms,
    )
    symbols = tuple(source_files)
    if args.max_symbols is not None:
        symbols = symbols[: args.max_symbols]
    print(f"pre-spike study: {len(symbols)} symbols", flush=True)
    jobs = [
        (
            symbol,
            source_files[symbol],
            args.train_start,
            args.train_end,
            args.test_start,
            args.test_end,
        )
        for symbol in symbols
    ]
    rows: list[dict[str, object]] = []
    bars_by_segment = {"train": 0, "test": 0}
    symbols_with_data = 0
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        for index, result in enumerate(
            pool.map(_process_symbol, jobs, chunksize=4), start=1
        ):
            if result is not None:
                symbols_with_data += 1
                rows.extend(result["rows"])
                for segment in bars_by_segment:
                    bars_by_segment[segment] += int(result["bars"][segment])
            if index % 50 == 0 or index == len(jobs):
                print(f"  processed {index}/{len(jobs)}", flush=True)

    if not rows:
        raise RuntimeError("no usable pre-spike samples found")
    summary = _aggregate(rows)
    selected = _select_condition(summary, args.min_train_alerts)
    block_bootstrap: dict[str, dict[str, float | int]] = {}
    if selected is not None:
        print(f"daily block-bootstrap pass: {selected}", flush=True)
        daily_jobs = [(*job, selected) for job in jobs]
        daily_rows: list[dict[str, object]] = []
        with ProcessPoolExecutor(max_workers=args.workers) as pool:
            for index, result in enumerate(
                pool.map(_process_selected_daily, daily_jobs, chunksize=4), start=1
            ):
                daily_rows.extend(result)
                if index % 100 == 0 or index == len(daily_jobs):
                    print(
                        f"  bootstrap data {index}/{len(daily_jobs)}",
                        flush=True,
                    )
        block_bootstrap = {
            segment: _block_bootstrap_by_day(daily_rows, segment=segment)
            for segment in ("train", "test")
        }
    elapsed = time.monotonic() - started
    report = _render_report(
        summary,
        symbols=len(symbols),
        symbols_with_data=symbols_with_data,
        bars_by_segment=bars_by_segment,
        train_start=args.train_start,
        train_end=args.train_end,
        test_start=args.test_start,
        test_end=args.test_end,
        elapsed_seconds=elapsed,
        min_train_alerts=args.min_train_alerts,
        block_bootstrap=block_bootstrap,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(report, encoding="utf-8")
    print(f"report: {args.out} ({elapsed:.1f}s)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
