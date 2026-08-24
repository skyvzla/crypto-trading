from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .analysis import analyze_factor
from .labels import horizon_label


@dataclass(frozen=True)
class SignalHorizonResult:
    factor: str
    target_prefix: str
    points: pd.DataFrame
    peak_horizon_seconds: int | None
    half_life_seconds: int | None


def analyze_signal_horizon(
    dataset: pd.DataFrame,
    factor: str,
    *,
    horizons_seconds: tuple[int, ...] = (300, 900, 1_800, 3_600),
    target_prefix: str = "short_return_",
) -> SignalHorizonResult:
    """观察因子对不同未来持有期收益的 Spearman IC 衰减。

    ``half_life_seconds`` 是离散研究周期上的近似值：从绝对 IC 峰值开始，第一次
    下降到峰值一半及以下的 horizon。若观测区间内没有衰减到一半则返回 None。
    """
    rows: list[dict[str, object]] = []
    for seconds in horizons_seconds:
        target = f"{target_prefix}{horizon_label(seconds)}"
        if target not in dataset.columns:
            continue
        result = analyze_factor(
            dataset,
            factor,
            target=target,
            quantiles=5,
            min_bucket_samples=10,
            bootstrap_resamples=0,
        )
        rows.append({
            "horizon_seconds": seconds,
            "target": target,
            "samples": result.samples,
            "spearman_ic": result.spearman_ic,
            "abs_spearman_ic": abs(result.spearman_ic)
            if np.isfinite(result.spearman_ic)
            else np.nan,
        })
    points = pd.DataFrame(rows)
    valid = points.dropna(subset=["abs_spearman_ic"]) if not points.empty else points
    if valid.empty:
        return SignalHorizonResult(factor, target_prefix, points, None, None)

    peak_row = valid.loc[valid["abs_spearman_ic"].idxmax()]
    peak_horizon = int(peak_row["horizon_seconds"])
    half_threshold = float(peak_row["abs_spearman_ic"]) / 2.0
    after_peak = valid[valid["horizon_seconds"] > peak_horizon].sort_values(
        "horizon_seconds", kind="stable"
    )
    half_candidates = after_peak[after_peak["abs_spearman_ic"] <= half_threshold]
    half_life = (
        None
        if half_candidates.empty
        else int(half_candidates.iloc[0]["horizon_seconds"])
    )
    return SignalHorizonResult(
        factor=factor,
        target_prefix=target_prefix,
        points=points,
        peak_horizon_seconds=peak_horizon,
        half_life_seconds=half_life,
    )
