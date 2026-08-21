from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .analysis import FactorAnalysisResult, analyze_factors
from .catalog import FactorSpec, available_factor_specs
from .correlation import factor_correlation_matrix, high_correlation_pairs
from .dataset import build_event_dataset
from .derivatives import attach_derivative_factors
from .event import SpikeEventConfig
from .horizon import SignalHorizonResult, analyze_signal_horizon
from .labels import SpikeLabelConfig
from .report import render_factor_report


@dataclass(frozen=True)
class FactorResearchResult:
    dataset: pd.DataFrame
    factor_specs: tuple[FactorSpec, ...]
    summary: pd.DataFrame
    details: dict[str, FactorAnalysisResult]
    horizon_results: dict[str, SignalHorizonResult]
    correlation_matrix: pd.DataFrame
    high_correlation_pairs: pd.DataFrame
    report: str


def run_factor_research(
    bars: pd.DataFrame,
    *,
    metrics: pd.DataFrame | None = None,
    event_config: SpikeEventConfig = SpikeEventConfig(),
    label_config: SpikeLabelConfig = SpikeLabelConfig(),
    target: str = "short_mfe_30m",
    factors: list[str] | tuple[str, ...] | None = None,
    include_scale_sensitive: bool = False,
    correlation_threshold: float = 0.8,
    min_bucket_samples: int = 10,
    event_start_ms: int | None = None,
    event_end_ms: int | None = None,
) -> FactorResearchResult:
    """执行 P1 的完整研究闭环，但不自动持久化任何 Dataset。"""
    dataset = build_event_dataset(
        bars,
        event_config=event_config,
        label_config=label_config,
        event_start_ms=event_start_ms,
        event_end_ms=event_end_ms,
    )
    if metrics is not None:
        dataset = attach_derivative_factors(dataset, metrics)

    return analyze_event_dataset(
        dataset,
        label_config=label_config,
        target=target,
        factors=factors,
        include_scale_sensitive=include_scale_sensitive,
        correlation_threshold=correlation_threshold,
        min_bucket_samples=min_bucket_samples,
    )


def analyze_event_dataset(
    dataset: pd.DataFrame,
    *,
    label_config: SpikeLabelConfig = SpikeLabelConfig(),
    target: str = "short_mfe_30m",
    factors: list[str] | tuple[str, ...] | None = None,
    include_scale_sensitive: bool = False,
    correlation_threshold: float = 0.8,
    min_bucket_samples: int = 10,
) -> FactorResearchResult:
    """分析已构建的事件级 Dataset；适合分块生成样本后统一统计。"""
    if target not in dataset.columns and not dataset.empty:
        raise ValueError(f"target is unavailable: {target}")

    available_specs = available_factor_specs(
        dataset, include_scale_sensitive=include_scale_sensitive
    )
    spec_by_name = {spec.name: spec for spec in available_specs}
    if factors is None:
        selected_specs = available_specs
    else:
        missing = [factor for factor in factors if factor not in dataset.columns]
        if missing:
            raise ValueError(f"requested factors are unavailable: {', '.join(missing)}")
        selected_specs = tuple(
            spec_by_name.get(
                factor,
                FactorSpec(factor, "custom", "用户指定自定义因子"),
            )
            for factor in factors
        )
    factor_names = [spec.name for spec in selected_specs]

    if factor_names:
        summary, details = analyze_factors(
            dataset,
            factor_names,
            target=target,
            min_bucket_samples=min_bucket_samples,
        )
        matrix = factor_correlation_matrix(dataset, factor_names)
        pairs = high_correlation_pairs(matrix, threshold=correlation_threshold)
        horizon_results = {
            factor: analyze_signal_horizon(
                dataset,
                factor,
                horizons_seconds=label_config.horizons_seconds,
            )
            for factor in factor_names
        }
        horizon_meta = pd.DataFrame([
            {
                "factor": factor,
                "peak_horizon_seconds": result.peak_horizon_seconds,
                "signal_half_life_seconds": result.half_life_seconds,
            }
            for factor, result in horizon_results.items()
        ])
        summary = summary.merge(horizon_meta, on="factor", how="left")
    else:
        summary = pd.DataFrame()
        details = {}
        matrix = pd.DataFrame()
        pairs = pd.DataFrame()
        horizon_results = {}

    report = render_factor_report(
        summary,
        details,
        target=target,
        event_count=len(dataset),
        correlation_pairs=pairs,
    )
    return FactorResearchResult(
        dataset=dataset,
        factor_specs=selected_specs,
        summary=summary,
        details=details,
        horizon_results=horizon_results,
        correlation_matrix=matrix,
        high_correlation_pairs=pairs,
        report=report,
    )
