"""对照式 lift 分析：以无条件 base rate 为基准评价因子与规则组合。

IC 只反映排序相关性，不回答"比随机好多少"。本模块全部输出都以
lift（条件均值 / 全体均值）为核心指标，并附带样本量、地形图分层、
阈值敏感性与 MFE/MAE 潜力诊断，用于因子初筛和显式规则组合。
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
import re

import numpy as np
import pandas as pd


_TARGET_HORIZON_RE = re.compile(r"_(\d+)(s|m|h)$")


def target_horizon_ms(target: str) -> int:
    """Return the forward label horizon encoded in a target column name."""
    match = _TARGET_HORIZON_RE.search(target)
    if match is None:
        raise ValueError(
            "target must end with a horizon such as 300s, 30m, or 1h"
        )
    value = int(match.group(1))
    if value <= 0:
        raise ValueError("target horizon must be positive")
    unit_ms = {"s": 1_000, "m": 60_000, "h": 3_600_000}[match.group(2)]
    return value * unit_ms


@dataclass(frozen=True)
class QuantileBand:
    """只允许在训练集拟合、之后原样应用到验证/测试集的分位区间。"""

    factor: str
    quantile: int
    quantiles: int
    lower: float | None
    upper: float | None


@dataclass(frozen=True)
class QuantilePairRule:
    """两因子分位规则；阈值一旦由训练集拟合便不可在测试集重算。"""

    left: QuantileBand
    right: QuantileBand


def _finite_target(dataset: pd.DataFrame, target: str) -> pd.Series:
    if target not in dataset.columns:
        raise ValueError(f"unknown target: {target}")
    values = pd.to_numeric(dataset[target], errors="coerce")
    return values.replace([np.inf, -np.inf], np.nan).dropna()


def _safe_lift(value: float, base: float) -> float:
    if not np.isfinite(value) or not np.isfinite(base) or base == 0:
        return float("nan")
    return value / base


def fit_quantile_band(
    dataset: pd.DataFrame,
    factor: str,
    *,
    quantile: int,
    quantiles: int = 3,
) -> QuantileBand:
    """在训练集上拟合一个值域分位区间。

    与直接在 test 上 ``qcut`` 不同，该对象保存的是训练期数值阈值，可用于真正的
    时间外推。对重复值较多的离散因子，边界可能重合；调用者应同时检查覆盖率。
    """
    if factor not in dataset.columns:
        raise ValueError(f"unknown factor: {factor}")
    if quantiles < 2:
        raise ValueError("quantiles must be at least 2")
    if not 1 <= quantile <= quantiles:
        raise ValueError("quantile must be within [1, quantiles]")
    values = pd.to_numeric(dataset[factor], errors="coerce").replace(
        [np.inf, -np.inf], np.nan
    ).dropna()
    if values.empty or values.nunique() < 2:
        raise ValueError(f"factor has insufficient finite variation: {factor}")
    edges = values.quantile(np.linspace(0.0, 1.0, quantiles + 1)).to_numpy(float)
    lower = None if quantile == 1 else float(edges[quantile - 1])
    upper = None if quantile == quantiles else float(edges[quantile])
    return QuantileBand(
        factor=factor,
        quantile=quantile,
        quantiles=quantiles,
        lower=lower,
        upper=upper,
    )


def apply_quantile_band(dataset: pd.DataFrame, band: QuantileBand) -> pd.Series:
    """把训练期拟合的区间应用到任意后续 Dataset。"""
    if band.factor not in dataset.columns:
        raise ValueError(f"unknown factor: {band.factor}")
    values = pd.to_numeric(dataset[band.factor], errors="coerce")
    mask = values.notna() & np.isfinite(values)
    if band.lower is not None:
        mask &= values.gt(band.lower)
    if band.upper is not None:
        mask &= values.le(band.upper)
    return mask


def fit_quantile_pair_rule(
    train: pd.DataFrame,
    *,
    factor_a: str,
    a_quantile: int,
    factor_b: str,
    b_quantile: int,
    quantiles: int = 3,
) -> QuantilePairRule:
    return QuantilePairRule(
        left=fit_quantile_band(
            train, factor_a, quantile=a_quantile, quantiles=quantiles
        ),
        right=fit_quantile_band(
            train, factor_b, quantile=b_quantile, quantiles=quantiles
        ),
    )


def apply_quantile_pair_rule(
    dataset: pd.DataFrame, rule: QuantilePairRule
) -> pd.Series:
    return apply_quantile_band(dataset, rule.left) & apply_quantile_band(
        dataset, rule.right
    )


def evaluate_time_oos_bands(
    dataset: pd.DataFrame,
    factors: list[str] | tuple[str, ...],
    *,
    split_ms: int,
    target: str,
    quantiles: int = 3,
    embargo_ms: int = 0,
) -> pd.DataFrame:
    """Fit edge quantile bands before ``split_ms`` and apply them unchanged after it."""
    if "timestamp_ms" not in dataset.columns:
        raise ValueError("dataset is missing timestamp_ms")
    if target not in dataset.columns:
        raise ValueError(f"unknown target: {target}")
    missing = [factor for factor in factors if factor not in dataset.columns]
    if missing:
        raise ValueError(f"unknown factors: {', '.join(missing)}")
    if quantiles < 2:
        raise ValueError("quantiles must be at least 2")
    if embargo_ms < 0:
        raise ValueError("embargo_ms must not be negative")
    effective_embargo_ms = max(embargo_ms, target_horizon_ms(target))
    train = dataset[
        dataset["timestamp_ms"].lt(split_ms - effective_embargo_ms)
    ]
    test = dataset[dataset["timestamp_ms"].ge(split_ms)]
    if train.empty or test.empty:
        raise ValueError("time OOS split requires non-empty train and test samples")

    target_values = pd.to_numeric(test.get(target), errors="coerce").replace(
        [np.inf, -np.inf], np.nan
    )
    target_valid = target_values.notna()
    base_mean = float(target_values[target_valid].mean())
    mfe_column = target if "mfe" in target else None
    mae_candidate = target.replace("mfe", "mae") if mfe_column else None
    mae_column = mae_candidate if mae_candidate in test.columns else None
    base_mfe = base_mean if mfe_column else float("nan")
    base_mae = (
        float(pd.to_numeric(test[mae_column], errors="coerce").mean())
        if mae_column
        else float("nan")
    )
    rows: list[dict[str, object]] = []
    for factor in factors:
        for quantile in (1, quantiles):
            try:
                band = fit_quantile_band(
                    train,
                    factor,
                    quantile=quantile,
                    quantiles=quantiles,
                )
            except ValueError:
                continue
            train_mask = apply_quantile_band(train, band)
            test_mask = apply_quantile_band(test, band)
            selected_target = target_values[test_mask & target_valid]
            selected_mean = (
                float(selected_target.mean())
                if len(selected_target)
                else float("nan")
            )
            selected_mae = (
                float(
                    pd.to_numeric(test.loc[test_mask, mae_column], errors="coerce").mean()
                )
                if mae_column
                else float("nan")
            )
            rows.append(
                {
                    "factor": factor,
                    "band": f"Q{quantile}/{quantiles}",
                    "lower": band.lower,
                    "upper": band.upper,
                    "train_samples": int(len(train)),
                    "train_selected": int(train_mask.sum()),
                    "test_samples": int(target_valid.sum()),
                    "test_selected": int((test_mask & target_valid).sum()),
                    "test_coverage": float((test_mask & target_valid).sum())
                    / max(1, int(target_valid.sum())),
                    "base_mean": base_mean,
                    "selected_mean": selected_mean,
                    "lift": _safe_lift(selected_mean, base_mean),
                    "base_mfe": base_mfe,
                    "selected_mfe": selected_mean if mfe_column else float("nan"),
                    "base_mae": base_mae,
                    "selected_mae": selected_mae,
                }
            )
    return pd.DataFrame(rows)


def render_time_oos_report(
    results: pd.DataFrame,
    *,
    split_ms: int,
    target: str,
    embargo_ms: int = 0,
) -> str:
    """Render the fixed-threshold time-forward validation table."""
    split = pd.Timestamp(split_ms, unit="ms", tz="UTC").isoformat()
    lines = [
        "# Factor Time OOS Report",
        "",
        f"- Split: `{split}`",
        f"- Target: `{target}`",
        f"- Train embargo: {embargo_ms / 60_000:g} minutes",
        "- Quantile thresholds are fitted on train only and applied unchanged to test.",
        "",
        "| Factor | Band | Train N | Train Selected | Test N | Test Selected | Coverage | Bounds | Base Mean | Selected Mean | Lift | Base MFE | Selected MFE | Base MAE | Selected MAE |",
        "|---|---:|---:|---:|---:|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in results.itertuples(index=False):
        lower = "-inf" if pd.isna(row.lower) else f"{row.lower:.6g}"
        upper = "+inf" if pd.isna(row.upper) else f"{row.upper:.6g}"
        values = [
            row.base_mean,
            row.selected_mean,
            row.lift,
            row.base_mfe,
            row.selected_mfe,
            row.base_mae,
            row.selected_mae,
        ]
        formatted = ["-" if pd.isna(value) else f"{value:.4f}" for value in values]
        lines.append(
            f"| `{row.factor}` | {row.band} | {row.train_samples} | "
            f"{row.train_selected} | {row.test_samples} | {row.test_selected} | "
            f"{row.test_coverage:.1%} | ({lower}, {upper}] | "
            + " | ".join(formatted)
            + " |"
        )
    lines.append("")
    return "\n".join(lines)


def _finite_pair(dataset: pd.DataFrame, factor: str, target: str) -> pd.DataFrame:
    pair = dataset[[factor, target]].copy()
    pair[factor] = pd.to_numeric(pair[factor], errors="coerce")
    pair[target] = pd.to_numeric(pair[target], errors="coerce")
    return pair.replace([np.inf, -np.inf], np.nan).dropna()


def base_rate_stats(dataset: pd.DataFrame, target: str) -> dict[str, float | int | None]:
    values = pd.to_numeric(dataset[target], errors="coerce") if target in dataset.columns else pd.Series(dtype=float)
    values = values.replace([np.inf, -np.inf], np.nan).dropna()
    success_rate: float | None = None
    if "success" in dataset.columns and len(values):
        success_rate = float(pd.to_numeric(dataset.loc[values.index, "success"], errors="coerce").mean())
    if not len(values):
        return {"samples": 0, "mean": float("nan"), "median": float("nan"), "success_rate": success_rate}
    return {
        "samples": int(len(values)),
        "mean": float(values.mean()),
        "median": float(values.median()),
        "success_rate": success_rate,
    }


def quantile_lift(
    dataset: pd.DataFrame,
    factor: str,
    target: str,
    quantiles: int = 5,
    min_bucket: int = 10,
) -> pd.DataFrame:
    columns = [
        "quantile",
        "samples",
        "mean",
        "median",
        "lift_mean",
        "lift_median",
        "lift_valid_mean",
        "lift_valid_median",
    ]
    pair = _finite_pair(dataset, factor, target)
    global_target = _finite_target(dataset, target)
    global_mean = float(global_target.mean()) if len(global_target) else float("nan")
    global_median = float(global_target.median()) if len(global_target) else float("nan")
    valid_mean = float(pair[target].mean()) if len(pair) else float("nan")
    valid_median = float(pair[target].median()) if len(pair) else float("nan")
    if len(pair) < quantiles or pair[factor].nunique() < 2:
        return pd.DataFrame(columns=columns)
    try:
        bins = pd.qcut(pair[factor].rank(method="average"), q=quantiles, labels=False, duplicates="drop")
    except ValueError:
        return pd.DataFrame(columns=columns)
    grouped = pair.assign(_bucket=bins).groupby("_bucket", observed=True)[target]
    frame = pd.DataFrame({
        "samples": grouped.count(),
        "mean": grouped.mean(),
        "median": grouped.median(),
    }).reset_index(names="quantile")
    frame["quantile"] = frame["quantile"].astype(int) + 1
    eligible = frame["samples"] >= min_bucket
    frame["lift_mean"] = np.where(
        eligible, frame["mean"].map(lambda value: _safe_lift(float(value), global_mean)), np.nan
    )
    frame["lift_median"] = np.where(
        eligible,
        frame["median"].map(lambda value: _safe_lift(float(value), global_median)),
        np.nan,
    )
    frame["lift_valid_mean"] = np.where(
        eligible, frame["mean"].map(lambda value: _safe_lift(float(value), valid_mean)), np.nan
    )
    frame["lift_valid_median"] = np.where(
        eligible,
        frame["median"].map(lambda value: _safe_lift(float(value), valid_median)),
        np.nan,
    )
    return frame[columns]


def scan_factor_lifts(
    dataset: pd.DataFrame,
    factors: list[str] | tuple[str, ...],
    target: str,
    quantiles: int = 5,
    min_bucket: int = 10,
) -> tuple[pd.DataFrame, dict[str, pd.DataFrame]]:
    rows: list[dict[str, object]] = []
    details: dict[str, pd.DataFrame] = {}
    total = max(1, len(dataset))
    for factor in factors:
        table = quantile_lift(dataset, factor, target, quantiles=quantiles, min_bucket=min_bucket)
        details[factor] = table
        valid = pd.to_numeric(dataset.get(factor), errors="coerce").notna().sum() if factor in dataset.columns else 0
        if table.empty:
            rows.append({
                "factor": factor,
                "samples": 0,
                "top_lift_mean": np.nan,
                "top_lift_median": np.nan,
                "bottom_lift_mean": np.nan,
                "top_lift_valid_mean": np.nan,
                "bottom_lift_valid_mean": np.nan,
                "best_lift_mean": np.nan,
                "best_side": None,
                "monotonic": False,
                "coverage": float(valid) / total,
            })
            continue
        lifts = table["lift_mean"].to_numpy(float)
        finite = lifts[np.isfinite(lifts)]
        monotonic = False
        if len(finite) >= 3:
            diffs = np.diff(finite)
            monotonic = bool(np.all(diffs >= 0) or np.all(diffs <= 0))
        top_lift = (
            float(table["lift_mean"].iloc[-1])
            if np.isfinite(table["lift_mean"].iloc[-1])
            else np.nan
        )
        bottom_lift = (
            float(table["lift_mean"].iloc[0])
            if np.isfinite(table["lift_mean"].iloc[0])
            else np.nan
        )
        candidates = {
            "top": top_lift,
            "bottom": bottom_lift,
        }
        finite_candidates = {
            side: value for side, value in candidates.items() if np.isfinite(value)
        }
        best_side = (
            max(finite_candidates, key=finite_candidates.get)
            if finite_candidates
            else None
        )
        rows.append({
            "factor": factor,
            "samples": int(table["samples"].sum()),
            "top_lift_mean": top_lift,
            "top_lift_median": float(table["lift_median"].iloc[-1]) if np.isfinite(table["lift_median"].iloc[-1]) else np.nan,
            "bottom_lift_mean": bottom_lift,
            "top_lift_valid_mean": float(table["lift_valid_mean"].iloc[-1]) if np.isfinite(table["lift_valid_mean"].iloc[-1]) else np.nan,
            "bottom_lift_valid_mean": float(table["lift_valid_mean"].iloc[0]) if np.isfinite(table["lift_valid_mean"].iloc[0]) else np.nan,
            "best_lift_mean": finite_candidates.get(best_side, np.nan),
            "best_side": best_side,
            "monotonic": monotonic,
            "coverage": float(valid) / total,
        })
    summary = pd.DataFrame(rows)
    if not summary.empty:
        summary = summary.sort_values(
            ["best_lift_mean", "coverage"],
            ascending=[False, False],
            na_position="last",
            kind="stable",
        ).reset_index(drop=True)
    return summary, details


def terrain_table(
    dataset: pd.DataFrame,
    target: str,
    rise_col: str = "rise_5s",
    volume_col: str = "volume_multiple_5s",
    rise_bins: tuple[float, ...] = (0.0, 0.03, 0.05, 0.08, 0.12, 0.20, np.inf),
    volume_bins: tuple[float, ...] = (0.0, 3.0, 5.0, 10.0, 20.0, 50.0, np.inf),
) -> pd.DataFrame:
    required = {rise_col, volume_col, target}
    missing = sorted(required - set(dataset.columns))
    if missing:
        raise ValueError(f"dataset missing terrain columns: {', '.join(missing)}")
    frame = dataset[list(required) + (["success"] if "success" in dataset.columns else [])].copy()
    for column in required:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.replace([np.inf, -np.inf], np.nan).dropna(subset=list(required))

    def _labels(edges: tuple[float, ...]) -> list[str]:
        labels = []
        for start, end in zip(edges[:-1], edges[1:]):
            left = "-inf" if np.isneginf(start) else f"{start:g}"
            right = "+inf" if np.isposinf(end) else f"{end:g}"
            labels.append(f"({left}, {right}]")
        return labels

    rise_labels = _labels(rise_bins)
    volume_labels = _labels(volume_bins)
    frame["_rise"] = pd.cut(frame[rise_col], bins=rise_bins, labels=rise_labels, right=True)
    frame["_vol"] = pd.cut(frame[volume_col], bins=volume_bins, labels=volume_labels, right=True)
    frame = frame.dropna(subset=["_rise", "_vol"])
    grouped = frame.groupby(["_rise", "_vol"], observed=True)
    result = grouped[target].agg(samples="count", mean="mean", median="median").reset_index()
    result = result.rename(columns={"_rise": "rise_bin", "_vol": "volume_bin"})
    if "success" in frame.columns:
        result["success_rate"] = grouped["success"].mean().to_numpy()
    return result


def rule_combination_lifts(
    dataset: pd.DataFrame,
    factors: list[str] | tuple[str, ...],
    target: str,
    quantiles: int = 3,
    max_pairs: int = 20,
    min_samples: int = 30,
) -> pd.DataFrame:
    columns = [
        "factor_a", "a_quantile", "factor_b", "b_quantile",
        "samples", "coverage", "hit_rate", "mean", "median", "lift_mean",
        "lift_valid_mean",
    ]
    usable = [f for f in factors if f in dataset.columns]
    if len(usable) < 2:
        return pd.DataFrame(columns=columns)
    global_target = _finite_target(dataset, target)
    global_mean = float(global_target.mean()) if len(global_target) else float("nan")
    if len(global_target) < min_samples:
        return pd.DataFrame(columns=columns)

    rows: list[dict[str, object]] = []
    for factor_a, factor_b in combinations(usable, 2):
        pair = dataset[[factor_a, factor_b, target]].copy()
        for column in (factor_a, factor_b, target):
            pair[column] = pd.to_numeric(pair[column], errors="coerce")
        pair = pair.replace([np.inf, -np.inf], np.nan).dropna()
        if len(pair) < min_samples:
            continue
        try:
            a_bins = pd.qcut(
                pair[factor_a].rank(method="average"),
                q=quantiles,
                labels=False,
                duplicates="drop",
            ).to_numpy()
            b_bins = pd.qcut(
                pair[factor_b].rank(method="average"),
                q=quantiles,
                labels=False,
                duplicates="drop",
            ).to_numpy()
        except ValueError:
            continue
        valid_mean = float(pair[target].mean())
        coverage = len(pair) / max(1, len(global_target))
        for i in range(quantiles):
            mask_a = a_bins == i
            if not mask_a.any():
                continue
            for j in range(quantiles):
                mask = mask_a & (b_bins == j)
                samples = int(mask.sum())
                if samples < min_samples:
                    continue
                values = pair.loc[mask, target]
                mean = float(values.mean())
                rows.append({
                    "factor_a": factor_a,
                    "a_quantile": i + 1,
                    "factor_b": factor_b,
                    "b_quantile": j + 1,
                    "samples": samples,
                    "coverage": coverage,
                    "hit_rate": float((values > 0).mean()),
                    "mean": mean,
                    "median": float(values.median()),
                    "lift_mean": _safe_lift(mean, global_mean),
                    "lift_valid_mean": _safe_lift(mean, valid_mean),
                })
    result = pd.DataFrame(rows, columns=columns)
    if result.empty:
        return result
    return result.sort_values("lift_mean", ascending=False, kind="stable").head(max_pairs).reset_index(drop=True)


def threshold_sensitivity(
    dataset: pd.DataFrame,
    factor: str,
    target: str,
    quantile_levels: tuple[float, ...] = (0.6, 0.7, 0.8, 0.9),
    min_samples: int = 20,
) -> pd.DataFrame:
    """观察同一方向在不同分位阈值下是否保持稳定，而不是只挑一个最优切点。"""
    columns = ["quantile", "threshold", "samples", "mean", "lift_mean"]
    pair = _finite_pair(dataset, factor, target)
    if pair.empty:
        return pd.DataFrame(columns=columns)
    global_target = _finite_target(dataset, target)
    global_mean = float(global_target.mean()) if len(global_target) else float("nan")
    rows: list[dict[str, object]] = []
    for p in quantile_levels:
        if not 0 < p < 1:
            raise ValueError("quantile levels must be within (0, 1)")
        threshold = float(pair[factor].quantile(p))
        selected = pair[pair[factor] > threshold][target]
        samples = int(len(selected))
        mean = float(selected.mean()) if samples else np.nan
        rows.append({
            "quantile": p,
            "threshold": threshold,
            "samples": samples,
            "mean": mean,
            "lift_mean": (
                _safe_lift(mean, global_mean) if samples >= min_samples else np.nan
            ),
        })
    return pd.DataFrame(rows, columns=columns)


def mfe_mae_potential_score(
    dataset: pd.DataFrame,
    rule_mask: pd.Series,
    mfe_col: str = "short_mfe_30m",
    mae_col: str = "short_mae_30m",
    fee_rate: float = 0.0005,
    slippage_rate: float = 0.0005,
) -> dict[str, float | int]:
    """仅用于潜力筛选的 MFE/MAE 诊断分数，**不是可交易期望**。

    MFE 和 MAE 不包含触达顺序；先止损再大幅回落的路径也可能同时拥有很高 MFE。
    因此该分数只能帮助筛掉明显差的规则，最终结论必须来自逐路径 SL/TP 回放。
    """
    empty = {
        "samples": 0,
        "p_favorable": float("nan"),
        "median_mfe": float("nan"),
        "median_mae": float("nan"),
        "potential_score": float("nan"),
    }
    if not len(dataset):
        return empty
    mask = pd.Series(rule_mask, index=dataset.index).fillna(False).astype(bool)
    mfe = pd.to_numeric(dataset.get(mfe_col), errors="coerce")
    mae = pd.to_numeric(dataset.get(mae_col), errors="coerce")
    selected = pd.concat([mfe[mask], mae[mask]], axis=1, keys=["mfe", "mae"]).dropna()
    if selected.empty:
        return empty
    cost = fee_rate + slippage_rate
    median_mfe = float(selected["mfe"].median())
    median_mae = float(selected["mae"].median())
    p_favorable = float((selected["mfe"] > cost).mean())
    potential_score = (
        p_favorable * median_mfe
        - (1 - p_favorable) * median_mae
        - cost
    )
    return {
        "samples": int(len(selected)),
        "p_favorable": p_favorable,
        "median_mfe": median_mfe,
        "median_mae": median_mae,
        "potential_score": potential_score,
    }


def render_lift_report(
    dataset: pd.DataFrame,
    factors: list[str] | tuple[str, ...],
    target: str,
    quantiles: int = 5,
    min_bucket: int = 10,
    min_rule_samples: int = 30,
) -> str:
    lines: list[str] = ["# Factor Lift Report", ""]
    stats = base_rate_stats(dataset, target)
    lines += [
        f"- target: `{target}`",
        f"- samples: {stats['samples']}",
        f"- base mean: {stats['mean']:.6f}" if isinstance(stats["mean"], float) and np.isfinite(stats["mean"]) else "- base mean: n/a",
        f"- base median: {stats['median']:.6f}" if isinstance(stats["median"], float) and np.isfinite(stats["median"]) else "- base median: n/a",
        f"- success rate: {stats['success_rate']:.2%}" if stats.get("success_rate") is not None else "- success rate: n/a",
        "",
    ]
    if dataset.empty or not factors:
        lines.append("无可用样本或因子，跳过 lift 分析。")
        return "\n".join(lines) + "\n"

    lines += ["## 地形图（rise × volume_multiple 分层）", ""]
    try:
        terrain = terrain_table(dataset, target)
        pivot = terrain.pivot_table(index="rise_bin", columns="volume_bin", values="samples", aggfunc="sum", observed=True)
        mean_pivot = terrain.pivot_table(index="rise_bin", columns="volume_bin", values="mean", aggfunc="mean", observed=True)
        lines += ["### 样本数", "", pivot.to_string(), "", "### 目标均值", "", mean_pivot.to_string(), ""]
    except ValueError as error:
        lines += [f"地形图不可用: {error}", ""]

    lines += ["## 单因子分位 lift 汇总", ""]
    summary, details = scan_factor_lifts(dataset, factors, target, quantiles=quantiles, min_bucket=min_bucket)
    if summary.empty:
        lines += ["无可分析因子。", ""]
    else:
        lines += [summary.to_string(index=False), ""]
        strong = summary.dropna(subset=["best_lift_mean"]).head(3)["factor"].tolist()
        for factor in strong:
            lines += [f"### {factor} 分位明细", "", details[factor].to_string(index=False), ""]
            sensitivity = threshold_sensitivity(dataset, factor, target)
            if not sensitivity.empty:
                lines += [f"### {factor} 阈值敏感性", "", sensitivity.to_string(index=False), ""]

    lines += ["## 两因子规则组合（top）", ""]
    rules = rule_combination_lifts(dataset, factors, target, min_samples=min_rule_samples)
    if rules.empty:
        lines += ["样本不足或因子不足，无规则组合输出。", ""]
    else:
        lines += [rules.head(10).to_string(index=False), ""]
        best = rules.iloc[0]
        fitted_rule = fit_quantile_pair_rule(
            dataset,
            factor_a=str(best["factor_a"]),
            a_quantile=int(best["a_quantile"]),
            factor_b=str(best["factor_b"]),
            b_quantile=int(best["b_quantile"]),
            quantiles=3,
        )
        mask = apply_quantile_pair_rule(dataset, fitted_rule)
        costs = mfe_mae_potential_score(dataset, mask)
        lines += [
            "## 最优规则 MFE/MAE 潜力诊断（非可交易期望）", "",
            f"- 规则: `{best['factor_a']}` Q{int(best['a_quantile'])} 且 `{best['factor_b']}` Q{int(best['b_quantile'])}",
            f"- samples: {costs['samples']}",
            f"- p_favorable: {costs['p_favorable']:.2%}" if np.isfinite(costs["p_favorable"]) else "- p_favorable: n/a",
            f"- median MFE: {costs['median_mfe']:.4f}" if np.isfinite(costs["median_mfe"]) else "- median MFE: n/a",
            f"- median MAE: {costs['median_mae']:.4f}" if np.isfinite(costs["median_mae"]) else "- median MAE: n/a",
            f"- potential score: {costs['potential_score']:.4f}" if np.isfinite(costs["potential_score"]) else "- potential score: n/a",
            "- 注意：MFE/MAE 不包含触达顺序；必须通过逐路径 SL/TP 回放才能得出交易期望。",
            "",
        ]
    return "\n".join(lines) + "\n"
