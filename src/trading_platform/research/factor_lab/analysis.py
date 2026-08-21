from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class FactorAnalysisResult:
    factor: str
    target: str
    samples: int
    pearson_ic: float
    spearman_ic: float
    bucket_ic_mean: float
    bucket_ic_std: float
    icir: float
    quantiles: pd.DataFrame

    def as_summary(self) -> dict[str, object]:
        return {
            "factor": self.factor,
            "target": self.target,
            "samples": self.samples,
            "pearson_ic": self.pearson_ic,
            "spearman_ic": self.spearman_ic,
            "bucket_ic_mean": self.bucket_ic_mean,
            "bucket_ic_std": self.bucket_ic_std,
            "icir": self.icir,
        }


def _finite_pair(frame: pd.DataFrame, factor: str, target: str) -> pd.DataFrame:
    if factor not in frame.columns:
        raise ValueError(f"unknown factor: {factor}")
    if target not in frame.columns:
        raise ValueError(f"unknown target: {target}")
    pair = frame[[factor, target, "timestamp_ms"]].copy()
    pair[factor] = pd.to_numeric(pair[factor], errors="coerce")
    pair[target] = pd.to_numeric(pair[target], errors="coerce")
    pair = pair.replace([np.inf, -np.inf], np.nan).dropna(subset=[factor, target])
    return pair


def _correlation(pair: pd.DataFrame, factor: str, target: str, method: str) -> float:
    if len(pair) < 3 or pair[factor].nunique() < 2 or pair[target].nunique() < 2:
        return float("nan")
    if method == "spearman":
        left = pair[factor].rank(method="average")
        right = pair[target].rank(method="average")
        return float(left.corr(right, method="pearson"))
    return float(pair[factor].corr(pair[target], method=method))


def analyze_factor(
    dataset: pd.DataFrame,
    factor: str,
    *,
    target: str = "short_mfe_30m",
    quantiles: int = 5,
    min_bucket_samples: int = 10,
) -> FactorAnalysisResult:
    """评价单因子 IC、月度 ICIR 和分位目标表现。"""
    if "timestamp_ms" not in dataset.columns:
        raise ValueError("dataset must contain timestamp_ms")
    if quantiles < 2:
        raise ValueError("quantiles must be at least 2")
    if min_bucket_samples < 3:
        raise ValueError("min_bucket_samples must be at least 3")

    pair = _finite_pair(dataset, factor, target)
    pearson = _correlation(pair, factor, target, "pearson")
    spearman = _correlation(pair, factor, target, "spearman")

    bucket_ics: list[float] = []
    if not pair.empty:
        bucket_key = pd.to_datetime(
            pair["timestamp_ms"], unit="ms", utc=True
        ).dt.strftime("%Y-%m")
        for _bucket, bucket in pair.groupby(bucket_key, sort=True):
            if len(bucket) < min_bucket_samples:
                continue
            value = _correlation(bucket, factor, target, "spearman")
            if np.isfinite(value):
                bucket_ics.append(value)
    if bucket_ics:
        bucket_mean = float(np.mean(bucket_ics))
        bucket_std = float(np.std(bucket_ics, ddof=1)) if len(bucket_ics) > 1 else float("nan")
    else:
        bucket_mean = bucket_std = float("nan")
    icir = (
        bucket_mean / bucket_std
        if np.isfinite(bucket_mean) and np.isfinite(bucket_std) and bucket_std > 0
        else float("nan")
    )

    quantile_frame = pd.DataFrame(columns=["quantile", "samples", "mean", "median"])
    if len(pair) >= quantiles and pair[factor].nunique() >= 2:
        try:
            ranks = pair[factor].rank(method="average")
            bins = pd.qcut(ranks, q=quantiles, labels=False, duplicates="drop")
            grouped = pair.assign(_quantile=bins).groupby("_quantile", observed=True)[target]
            quantile_frame = pd.DataFrame({
                "samples": grouped.count(),
                "mean": grouped.mean(),
                "median": grouped.median(),
            }).reset_index(names="quantile")
            quantile_frame["quantile"] = quantile_frame["quantile"].astype(int) + 1
        except ValueError:
            pass

    return FactorAnalysisResult(
        factor=factor,
        target=target,
        samples=len(pair),
        pearson_ic=pearson,
        spearman_ic=spearman,
        bucket_ic_mean=bucket_mean,
        bucket_ic_std=bucket_std,
        icir=icir,
        quantiles=quantile_frame,
    )


def analyze_factors(
    dataset: pd.DataFrame,
    factors: list[str] | tuple[str, ...],
    *,
    target: str = "short_mfe_30m",
    quantiles: int = 5,
    min_bucket_samples: int = 10,
) -> tuple[pd.DataFrame, dict[str, FactorAnalysisResult]]:
    """批量分析，并按 |Spearman IC| 降序给出摘要。"""
    results = {
        factor: analyze_factor(
            dataset,
            factor,
            target=target,
            quantiles=quantiles,
            min_bucket_samples=min_bucket_samples,
        )
        for factor in factors
    }
    summary = pd.DataFrame([result.as_summary() for result in results.values()])
    if not summary.empty:
        summary["abs_spearman_ic"] = summary["spearman_ic"].abs()
        summary = summary.sort_values(
            ["abs_spearman_ic", "samples"], ascending=[False, False], kind="stable"
        ).reset_index(drop=True)
    return summary, results
