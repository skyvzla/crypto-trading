from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class SpikeEventConfig:
    """与当前 spike 策略相近的候选事件定义。"""

    rise_threshold: float = 0.05
    volume_multiple_threshold: float = 5.0
    cooldown_seconds: int = 60
    require_orderflow: bool = False

    def __post_init__(self) -> None:
        if self.rise_threshold <= 0:
            raise ValueError("rise_threshold must be positive")
        if self.volume_multiple_threshold <= 0:
            raise ValueError("volume_multiple_threshold must be positive")
        if self.cooldown_seconds < 0:
            raise ValueError("cooldown_seconds cannot be negative")


def detect_spike_events(
    factor_frame: pd.DataFrame,
    *,
    config: SpikeEventConfig = SpikeEventConfig(),
) -> pd.DataFrame:
    """从因果 Factor Frame 中提取事件触发时点快照。

    相邻秒持续满足条件时只保留 cooldown 内的第一个触发点，避免把同一次插针
    拆成大量高度相关样本。
    """
    required = {"symbol", "timestamp_ms", "rise_5s", "volume_multiple_5s"}
    missing = sorted(required - set(factor_frame.columns))
    if missing:
        raise ValueError(f"factor frame missing event columns: {', '.join(missing)}")
    if factor_frame.empty:
        result = factor_frame.copy()
        result.insert(0, "event_id", pd.Series(dtype="string"))
        return result

    mask = (
        factor_frame["rise_5s"].ge(config.rise_threshold)
        & factor_frame["volume_multiple_5s"].ge(config.volume_multiple_threshold)
    )
    if "continuous_61s" in factor_frame.columns:
        mask &= factor_frame["continuous_61s"].fillna(False)
    if config.require_orderflow:
        required_orderflow = {"taker_buy_ratio_5s", "cvd_5s"}
        missing_orderflow = sorted(required_orderflow - set(factor_frame.columns))
        if missing_orderflow:
            raise ValueError(
                "factor frame missing required orderflow columns: "
                + ", ".join(missing_orderflow)
            )
        mask &= factor_frame["taker_buy_ratio_5s"].notna()
        mask &= factor_frame["cvd_5s"].notna()

    candidates = factor_frame.loc[mask].sort_values(
        ["symbol", "timestamp_ms"], kind="stable"
    )
    if candidates.empty:
        result = candidates.copy()
        result.insert(0, "event_id", pd.Series(dtype="string"))
        return result

    keep_indexes: list[int] = []
    cooldown_ms = config.cooldown_seconds * 1_000
    for _symbol, group in candidates.groupby("symbol", sort=False):
        previous_candidate: int | None = None
        for index, row in group.iterrows():
            timestamp = int(row["timestamp_ms"])
            if previous_candidate is None or timestamp - previous_candidate > cooldown_ms:
                keep_indexes.append(index)
            previous_candidate = timestamp

    events = factor_frame.loc[keep_indexes].copy().sort_values(
        ["timestamp_ms", "symbol"], kind="stable"
    )
    events.insert(
        0,
        "event_id",
        events.apply(
            lambda row: f"{row['symbol']}:{int(row['timestamp_ms'])}", axis=1
        ),
    )
    return events.reset_index(drop=True)
