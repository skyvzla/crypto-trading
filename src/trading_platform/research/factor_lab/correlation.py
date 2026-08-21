from __future__ import annotations

import numpy as np
import pandas as pd


def factor_correlation_matrix(
    dataset: pd.DataFrame,
    factors: list[str] | tuple[str, ...],
    *,
    method: str = "spearman",
) -> pd.DataFrame:
    missing = [factor for factor in factors if factor not in dataset.columns]
    if missing:
        raise ValueError(f"unknown factors: {', '.join(missing)}")
    numeric = dataset[list(factors)].apply(pd.to_numeric, errors="coerce")
    numeric = numeric.replace([np.inf, -np.inf], np.nan)
    if method == "spearman":
        numeric = numeric.rank(method="average")
        method = "pearson"
    return numeric.corr(method=method)


def high_correlation_pairs(
    matrix: pd.DataFrame,
    *,
    threshold: float = 0.8,
) -> pd.DataFrame:
    if not 0 < threshold <= 1:
        raise ValueError("threshold must be in (0, 1]")
    rows: list[dict[str, object]] = []
    columns = list(matrix.columns)
    for left_index, left in enumerate(columns):
        for right in columns[left_index + 1 :]:
            value = matrix.loc[left, right]
            if pd.notna(value) and abs(float(value)) >= threshold:
                rows.append({
                    "factor_a": left,
                    "factor_b": right,
                    "correlation": float(value),
                    "abs_correlation": abs(float(value)),
                })
    result = pd.DataFrame(rows)
    if not result.empty:
        result = result.sort_values(
            "abs_correlation", ascending=False, kind="stable"
        ).reset_index(drop=True)
    return result
