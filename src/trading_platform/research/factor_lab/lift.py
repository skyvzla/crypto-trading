"""对照式 lift 分析：以无条件 base rate 为基准评价因子与规则组合。

IC 只反映排序相关性，不回答"比随机好多少"。本模块全部输出都以
lift（条件均值 / 全体均值）为核心指标，并附带样本量、地形图分层、
阈值敏感性与扣费后期望，用于因子初筛和显式规则组合。
"""

from __future__ import annotations

from itertools import combinations

import numpy as np
import pandas as pd


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
    columns = ["quantile", "samples", "mean", "median", "lift_mean", "lift_median"]
    pair = _finite_pair(dataset, factor, target)
    overall_mean = float(pair[target].mean()) if len(pair) else float("nan")
    overall_median = float(pair[target].median()) if len(pair) else float("nan")
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
    frame["lift_mean"] = np.where(frame["samples"] >= min_bucket, frame["mean"] / overall_mean, np.nan)
    frame["lift_median"] = np.where(frame["samples"] >= min_bucket, frame["median"] / overall_median, np.nan)
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
        rows.append({
            "factor": factor,
            "samples": int(table["samples"].sum()),
            "top_lift_mean": float(table["lift_mean"].iloc[-1]) if np.isfinite(table["lift_mean"].iloc[-1]) else np.nan,
            "top_lift_median": float(table["lift_median"].iloc[-1]) if np.isfinite(table["lift_median"].iloc[-1]) else np.nan,
            "bottom_lift_mean": float(table["lift_mean"].iloc[0]) if np.isfinite(table["lift_mean"].iloc[0]) else np.nan,
            "monotonic": monotonic,
            "coverage": float(valid) / total,
        })
    summary = pd.DataFrame(rows)
    if not summary.empty:
        summary = summary.sort_values(
            "top_lift_mean", ascending=False, na_position="last", kind="stable"
        ).reset_index(drop=True)
    return summary, details


def terrain_table(
    dataset: pd.DataFrame,
    target: str,
    rise_col: str = "rise_5s",
    volume_col: str = "volume_multiple_5s",
    rise_bins: tuple[float, ...] = (0.05, 0.08, 0.12, 0.20, 1e9),
    volume_bins: tuple[float, ...] = (5.0, 10.0, 20.0, 50.0, 1e18),
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
            left = f"{start:g}" if start < 1e8 else f"{start:g}"
            right = "+inf" if end >= 1e8 else f"{end:g}"
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
        "samples", "hit_rate", "mean", "median", "lift_mean",
    ]
    usable = [f for f in factors if f in dataset.columns]
    if len(usable) < 2:
        return pd.DataFrame(columns=columns)
    pair_all = dataset[usable + [target]].copy()
    for column in usable + [target]:
        pair_all[column] = pd.to_numeric(pair_all[column], errors="coerce")
    pair_all = pair_all.replace([np.inf, -np.inf], np.nan).dropna()
    if len(pair_all) < min_samples:
        return pd.DataFrame(columns=columns)
    overall_mean = float(pair_all[target].mean())

    buckets: dict[str, np.ndarray] = {}
    for factor in usable:
        try:
            bins = pd.qcut(pair_all[factor].rank(method="average"), q=quantiles, labels=False, duplicates="drop")
        except ValueError:
            continue
        buckets[factor] = bins.to_numpy()
    usable = [f for f in usable if f in buckets]

    rows: list[dict[str, object]] = []
    for factor_a, factor_b in combinations(usable, 2):
        a_bins, b_bins = buckets[factor_a], buckets[factor_b]
        for i in range(quantiles):
            mask_a = a_bins == i
            if not mask_a.any():
                continue
            for j in range(quantiles):
                mask = mask_a & (b_bins == j)
                samples = int(mask.sum())
                if samples < min_samples:
                    continue
                values = pair_all.loc[mask, target]
                mean = float(values.mean())
                rows.append({
                    "factor_a": factor_a,
                    "a_quantile": i + 1,
                    "factor_b": factor_b,
                    "b_quantile": j + 1,
                    "samples": samples,
                    "hit_rate": float((values > 0).mean()),
                    "mean": mean,
                    "median": float(values.median()),
                    "lift_mean": mean / overall_mean if overall_mean else np.nan,
                })
    result = pd.DataFrame(rows, columns=columns)
    if result.empty:
        return result
    return result.sort_values("lift_mean", ascending=False, kind="stable").head(max_pairs).reset_index(drop=True)


def threshold_sensitivity(
    dataset: pd.DataFrame,
    factor: str,
    target: str,
    base_quantile: float = 0.8,
    perturbations: tuple[float, ...] = (0.6, 0.7, 0.8, 0.9),
    min_samples: int = 20,
) -> pd.DataFrame:
    columns = ["perturbation", "threshold", "samples", "mean", "lift_mean"]
    pair = _finite_pair(dataset, factor, target)
    if pair.empty:
        return pd.DataFrame(columns=columns)
    overall_mean = float(pair[target].mean())
    rows: list[dict[str, object]] = []
    for p in perturbations:
        threshold = float(pair[factor].quantile(p))
        selected = pair[pair[factor] > threshold][target]
        samples = int(len(selected))
        mean = float(selected.mean()) if samples else np.nan
        rows.append({
            "perturbation": p,
            "threshold": threshold,
            "samples": samples,
            "mean": mean,
            "lift_mean": mean / overall_mean if samples and overall_mean else np.nan,
        })
    return pd.DataFrame(rows, columns=columns)


def cost_adjusted_expectancy(
    dataset: pd.DataFrame,
    rule_mask: pd.Series,
    mfe_col: str = "short_mfe_30m",
    mae_col: str = "short_mae_30m",
    fee_rate: float = 0.0005,
    slippage_rate: float = 0.0005,
) -> dict[str, float | int]:
    """做空视角的扣费期望近似：p_win×avg_win−(1−p_win)×avg_loss−费用。"""
    empty = {"samples": 0, "p_win": float("nan"), "avg_win": float("nan"), "avg_loss": float("nan"), "expectancy": float("nan")}
    if not len(dataset):
        return empty
    mask = pd.Series(rule_mask, index=dataset.index).fillna(False).astype(bool)
    mfe = pd.to_numeric(dataset.get(mfe_col), errors="coerce")
    mae = pd.to_numeric(dataset.get(mae_col), errors="coerce")
    selected = pd.concat([mfe[mask], mae[mask]], axis=1, keys=["mfe", "mae"]).dropna()
    if selected.empty:
        return empty
    cost = fee_rate + slippage_rate
    avg_win = float(selected["mfe"].median())
    avg_loss = float(selected["mae"].median())
    p_win = float((selected["mfe"] > cost).mean())
    expectancy = p_win * avg_win - (1 - p_win) * avg_loss - cost
    return {
        "samples": int(len(selected)),
        "p_win": p_win,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "expectancy": expectancy,
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
        strong = summary.dropna(subset=["top_lift_mean"]).head(3)["factor"].tolist()
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
        mask = (
            pd.qcut(pd.to_numeric(dataset[best["factor_a"]], errors="coerce").rank(method="average"), q=3, labels=False, duplicates="drop").eq(int(best["a_quantile"]) - 1)
            & pd.qcut(pd.to_numeric(dataset[best["factor_b"]], errors="coerce").rank(method="average"), q=3, labels=False, duplicates="drop").eq(int(best["b_quantile"]) - 1)
        )
        costs = cost_adjusted_expectancy(dataset, mask)
        lines += [
            "## 最优规则扣费期望（近似，做空视角）", "",
            f"- 规则: `{best['factor_a']}` Q{int(best['a_quantile'])} 且 `{best['factor_b']}` Q{int(best['b_quantile'])}",
            f"- samples: {costs['samples']}",
            f"- p_win: {costs['p_win']:.2%}" if np.isfinite(costs["p_win"]) else "- p_win: n/a",
            f"- avg_win(mfe中位): {costs['avg_win']:.4f}" if np.isfinite(costs["avg_win"]) else "- avg_win: n/a",
            f"- avg_loss(mae中位): {costs['avg_loss']:.4f}" if np.isfinite(costs["avg_loss"]) else "- avg_loss: n/a",
            f"- expectancy: {costs['expectancy']:.4f}" if np.isfinite(costs["expectancy"]) else "- expectancy: n/a",
            "",
        ]
    return "\n".join(lines) + "\n"
