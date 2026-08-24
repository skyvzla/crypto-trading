from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


MS_PER_MINUTE = 60_000


@dataclass(frozen=True)
class PreSpikeCondition:
    """可复现的 pre-spike 数值规则；不允许在测试段重新拟合阈值。"""

    name: str
    thresholds: tuple[tuple[str, float], ...]

    @property
    def label(self) -> str:
        if not self.thresholds:
            return self.name
        values = ", ".join(f"{name}>{value:g}" for name, value in self.thresholds)
        return f"{self.name} ({values})"


def add_pre_spike_factors(frame: pd.DataFrame) -> pd.DataFrame:
    """由 1m OHLCV 生成纯历史 pre-event 因子，并在数据缺口处重置滚动状态。"""
    required = {"open_ms", "open", "high", "low", "close", "volume"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"1m frame missing columns: {', '.join(missing)}")
    if frame.empty:
        return frame.copy()

    ordered = frame.sort_values("open_ms", kind="stable").reset_index(drop=True)
    if ordered["open_ms"].duplicated().any():
        raise ValueError("1m frame contains duplicate timestamps")
    timestamps = pd.to_numeric(ordered["open_ms"], errors="raise").to_numpy(np.int64)
    segment_id = pd.Series(
        np.r_[True, np.diff(timestamps) != MS_PER_MINUTE]
    ).cumsum()

    parts: list[pd.DataFrame] = []
    for segment_number, (_segment, source) in enumerate(
        ordered.groupby(segment_id, sort=False), start=1
    ):
        group = source.copy().reset_index(drop=True)
        group["segment_id"] = segment_number
        high = pd.to_numeric(group["high"], errors="coerce")
        low = pd.to_numeric(group["low"], errors="coerce")
        close = pd.to_numeric(group["close"], errors="coerce")
        volume = pd.to_numeric(group["volume"], errors="coerce")

        previous_close = close.shift(1)
        true_range = pd.concat(
            [
                high.sub(low),
                high.sub(previous_close).abs(),
                low.sub(previous_close).abs(),
            ],
            axis=1,
        ).max(axis=1)
        atr = true_range.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
        atr_ratio = atr.div(close.where(close > 0))
        atr_baseline = atr_ratio.shift(1).rolling(30, min_periods=30).median()
        group["atr_mult"] = atr_ratio.div(atr_baseline.where(atr_baseline > 0))

        typical = high.add(low).add(close).div(3.0)
        mean = typical.rolling(20, min_periods=20).mean()
        std = typical.rolling(20, min_periods=20).std(ddof=1)
        bbw = std.mul(4.0).div(mean.where(mean > 0))
        bbw_baseline = bbw.shift(1).rolling(30, min_periods=30).median()
        group["bbw_mult"] = bbw.div(bbw_baseline.where(bbw_baseline > 0))

        group["wick_pct"] = high.div(close.where(close > 0)).sub(1.0).mul(100.0)
        volume_baseline = volume.shift(1).rolling(30, min_periods=30).median()
        group["volume_mult"] = volume.div(volume_baseline.where(volume_baseline > 0))
        group["return_5m"] = close.div(close.shift(5).where(close.shift(5) > 0)).sub(1.0)
        group["spike_15pct"] = high.ge(previous_close.mul(1.15)).fillna(False)
        parts.append(group)

    return pd.concat(parts, ignore_index=True).sort_values(
        "open_ms", kind="stable"
    ).reset_index(drop=True)


def future_event_labels(
    event_mask: pd.Series | np.ndarray,
    horizons: tuple[int, ...],
    *,
    segment_ids: pd.Series | np.ndarray | None = None,
) -> dict[int, tuple[np.ndarray, np.ndarray]]:
    """返回 ``horizon -> (future_hit, eligible)``，末端不足完整窗口的样本被删失。"""
    events = np.asarray(event_mask, dtype=bool)
    size = len(events)
    segments = None if segment_ids is None else np.asarray(segment_ids)
    if segments is not None and len(segments) != size:
        raise ValueError("segment_ids must have the same length as event_mask")
    result: dict[int, tuple[np.ndarray, np.ndarray]] = {}
    for horizon in horizons:
        if horizon <= 0:
            raise ValueError("horizons must be positive")
        hit = np.zeros(size, dtype=bool)
        for offset in range(1, horizon + 1):
            if offset >= size:
                break
            candidate = events[offset:]
            if segments is not None:
                candidate = candidate & (segments[:-offset] == segments[offset:])
            hit[:-offset] |= candidate
        eligible = np.arange(size, dtype=np.int64) + horizon < size
        if segments is not None and horizon < size:
            eligible[:-horizon] &= segments[:-horizon] == segments[horizon:]
            eligible[-horizon:] = False
        result[horizon] = (hit, eligible)
    return result


def cooldown_alert_mask(
    condition: pd.Series | np.ndarray,
    *,
    cooldown_bars: int,
    segment_ids: pd.Series | np.ndarray | None = None,
) -> np.ndarray:
    """把连续/密集前兆压成 alert episode，避免同一状态每分钟重复计样本。"""
    if cooldown_bars < 0:
        raise ValueError("cooldown_bars cannot be negative")
    candidates = np.asarray(condition, dtype=bool)
    segments = None if segment_ids is None else np.asarray(segment_ids)
    if segments is not None and len(segments) != len(candidates):
        raise ValueError("segment_ids must have the same length as condition")
    alerts = np.zeros(len(candidates), dtype=bool)
    previous_candidate: int | None = None
    previous_segment: object | None = None
    for index in np.flatnonzero(candidates):
        current_segment = None if segments is None else segments[index]
        if (
            previous_candidate is None
            or current_segment != previous_segment
            or index - previous_candidate > cooldown_bars
        ):
            alerts[index] = True
        previous_candidate = int(index)
        previous_segment = current_segment
    return alerts


def recent_event_mask(
    event_mask: pd.Series | np.ndarray,
    *,
    lookback_bars: int,
    segment_ids: pd.Series | np.ndarray | None = None,
) -> np.ndarray:
    """当前及过去 lookback 根是否已有事件，用于排除“事件发生后才预警”。"""
    if lookback_bars < 0:
        raise ValueError("lookback_bars cannot be negative")
    events = np.asarray(event_mask, dtype=bool)
    segments = None if segment_ids is None else np.asarray(segment_ids)
    if segments is not None and len(segments) != len(events):
        raise ValueError("segment_ids must have the same length as event_mask")
    recent = events.copy()
    for offset in range(1, lookback_bars + 1):
        if offset >= len(events):
            break
        prior = events[:-offset]
        if segments is not None:
            prior = prior & (segments[offset:] == segments[:-offset])
        recent[offset:] |= prior
    return recent


def event_capture_stats(
    alerts: pd.Series | np.ndarray,
    events: pd.Series | np.ndarray,
    *,
    horizon_bars: int,
    eligible_events: pd.Series | np.ndarray | None = None,
    segment_ids: pd.Series | np.ndarray | None = None,
) -> dict[str, float | int]:
    """事件级 recall 与最近一次有效预警的平均提前量。"""
    if horizon_bars <= 0:
        raise ValueError("horizon_bars must be positive")
    alert_values = np.asarray(alerts, dtype=bool)
    event_values = np.asarray(events, dtype=bool)
    if len(alert_values) != len(event_values):
        raise ValueError("alerts and events must have the same length")
    segments = None if segment_ids is None else np.asarray(segment_ids)
    if segments is not None and len(segments) != len(event_values):
        raise ValueError("segment_ids must have the same length as events")
    eligible = (
        np.ones(len(event_values), dtype=bool)
        if eligible_events is None
        else np.asarray(eligible_events, dtype=bool)
    )
    event_indexes = np.flatnonzero(event_values & eligible)
    captured = 0
    lead_sum = 0.0
    for event_index in event_indexes:
        start = max(0, int(event_index) - horizon_bars)
        candidates = np.flatnonzero(alert_values[start:event_index]) + start
        if segments is not None and len(candidates):
            candidates = candidates[segments[candidates] == segments[event_index]]
        if not len(candidates):
            continue
        captured += 1
        lead_sum += int(event_index) - int(candidates[-1])
    return {
        "events": int(len(event_indexes)),
        "captured_events": captured,
        "recall": captured / len(event_indexes) if len(event_indexes) else float("nan"),
        "lead_sum_bars": lead_sum,
        "mean_lead_bars": lead_sum / captured if captured else float("nan"),
    }


def condition_mask(frame: pd.DataFrame, condition: PreSpikeCondition) -> np.ndarray:
    mask = np.ones(len(frame), dtype=bool)
    for factor, threshold in condition.thresholds:
        if factor not in frame.columns:
            raise ValueError(f"unknown pre-spike factor: {factor}")
        values = pd.to_numeric(frame[factor], errors="coerce").to_numpy(float)
        mask &= np.isfinite(values) & (values > threshold)
    return mask


def wilson_interval(hits: int, samples: int, z: float = 1.959963984540054) -> tuple[float, float]:
    if samples <= 0:
        return float("nan"), float("nan")
    probability = hits / samples
    denominator = 1.0 + z * z / samples
    center = (probability + z * z / (2.0 * samples)) / denominator
    margin = (
        z
        * np.sqrt(
            probability * (1.0 - probability) / samples
            + z * z / (4.0 * samples * samples)
        )
        / denominator
    )
    return float(max(0.0, center - margin)), float(min(1.0, center + margin))

