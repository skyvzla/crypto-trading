from __future__ import annotations

from collections.abc import Mapping

import pandas as pd

from .analysis import FactorAnalysisResult


def _format_float(value: object) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "-"
    if pd.isna(number):
        return "-"
    return f"{number:.4f}"


def render_factor_report(
    summary: pd.DataFrame,
    details: Mapping[str, FactorAnalysisResult],
    *,
    target: str,
    event_count: int,
    correlation_pairs: pd.DataFrame | None = None,
) -> str:
    """生成轻量 Markdown 报告；调用者决定是否落盘。"""
    lines = [
        "# Spike Factor Lab Report",
        "",
        f"- 事件样本：{event_count}",
        f"- 目标标签：`{target}`",
        "- 因子值只使用事件时点及之前数据；未来窗口只用于标签。",
        "",
        "## 因子摘要",
        "",
        "| Factor | Samples | Pearson IC | Spearman IC | ICIR | Peak Horizon | Half-life |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in summary.itertuples(index=False):
        peak = getattr(row, "peak_horizon_seconds", None)
        half_life = getattr(row, "signal_half_life_seconds", None)
        peak_text = "-" if pd.isna(peak) else f"{int(peak)}s"
        half_life_text = "-" if pd.isna(half_life) else f"{int(half_life)}s"
        lines.append(
            f"| `{row.factor}` | {int(row.samples)} | {_format_float(row.pearson_ic)} | "
            f"{_format_float(row.spearman_ic)} | {_format_float(row.icir)} | "
            f"{peak_text} | {half_life_text} |"
        )

    if correlation_pairs is not None:
        lines.extend(["", "## 高相关因子", ""])
        if correlation_pairs.empty:
            lines.append("未发现达到阈值的高相关因子对。")
        else:
            lines.extend([
                "| Factor A | Factor B | Correlation |",
                "|---|---|---:|",
            ])
            for row in correlation_pairs.itertuples(index=False):
                lines.append(
                    f"| `{row.factor_a}` | `{row.factor_b}` | "
                    f"{_format_float(row.correlation)} |"
                )

    for factor in summary["factor"].tolist() if "factor" in summary.columns else []:
        result = details.get(str(factor))
        if result is None or result.quantiles.empty:
            continue
        lines.extend(["", f"## `{factor}` 分位表现", ""])
        lines.extend([
            "| Quantile | Samples | Mean Target | Median Target |",
            "|---:|---:|---:|---:|",
        ])
        for row in result.quantiles.itertuples(index=False):
            lines.append(
                f"| Q{int(row.quantile)} | {int(row.samples)} | "
                f"{_format_float(row.mean)} | {_format_float(row.median)} |"
            )
    lines.append("")
    return "\n".join(lines)
