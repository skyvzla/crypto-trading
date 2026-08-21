"""Spike 事件因子研究工具。

该包只服务离线研究：读取现有归档、按需派生因子、生成事件标签和统计报告。
它不参与实时交易，也不要求把派生因子写回长期 1s 归档。
"""

from .analysis import FactorAnalysisResult, analyze_factor, analyze_factors
from .catalog import FactorSpec, available_factor_specs
from .correlation import factor_correlation_matrix, high_correlation_pairs
from .dataset import build_event_dataset, build_factor_frame, load_bar1s_frame
from .derivatives import (
    add_derivative_factors,
    attach_derivative_factors,
    load_metrics_frame,
)
from .event import SpikeEventConfig, detect_spike_events
from .horizon import SignalHorizonResult, analyze_signal_horizon
from .labels import SpikeLabelConfig, attach_short_labels, horizon_label
from .report import render_factor_report
from .workflow import FactorResearchResult, analyze_event_dataset, run_factor_research

__all__ = [
    "FactorAnalysisResult",
    "FactorResearchResult",
    "FactorSpec",
    "SpikeEventConfig",
    "SpikeLabelConfig",
    "SignalHorizonResult",
    "analyze_factor",
    "analyze_event_dataset",
    "analyze_factors",
    "analyze_signal_horizon",
    "available_factor_specs",
    "add_derivative_factors",
    "attach_short_labels",
    "attach_derivative_factors",
    "build_event_dataset",
    "build_factor_frame",
    "detect_spike_events",
    "factor_correlation_matrix",
    "high_correlation_pairs",
    "horizon_label",
    "load_bar1s_frame",
    "load_metrics_frame",
    "render_factor_report",
    "run_factor_research",
]
