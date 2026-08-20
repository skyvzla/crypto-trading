"""Spike 入场评分：多维特征倒 U 评分 + 准入 + 动态溢价。

数据标定来源：``reports/research_record_all.csv``（2664 个真实 1s 触发信号，
含触发时点特征与 4h 回落结果）。核心发现：所有动量特征与做空收益呈
"倒 U" 关系——动能极低（Q1）与极端超买（Q5）收益差，Q3-Q4 最优。

评分设计：
- 每个维度按倒 U 映射到 0~1（P20->0, P40->0.6, P60->1.0, P80->0.7, P95->0.2）
- S = Σ w_i × score_i（权重可配置，默认已去耦合）
- 准入：S >= threshold
- 溢价：premium_pct = base + S × 模型预测冲高% × mult（无上限，由配置 cap）
"""

from __future__ import annotations

from decimal import Decimal


def inverted_u_score(
    x: float,
    *,
    p20: float,
    p40: float,
    p60: float,
    p80: float,
    p95: float,
) -> float:
    """倒 U 映射：P20->0, P40->0.6, P60->1.0, P80->0.7, P95->0.2。

    分段线性插值；x 低于 p20 记 0，高于 p95 记 0.2。
    边界值须满足 p20 < p40 < p60 < p80 < p95。
    """
    if p20 >= p40 or p40 >= p60 or p60 >= p80 or p80 >= p95:
        raise ValueError("inverted_u_score boundaries must be strictly increasing")
    if x <= p20:
        return 0.0
    if x >= p95:
        return 0.2
    if x <= p40:
        return 0.6 * (x - p20) / (p40 - p20)
    if x <= p60:
        return 0.6 + 0.4 * (x - p40) / (p60 - p40)
    if x <= p80:
        return 1.0 - 0.3 * (x - p60) / (p80 - p60)
    return 0.7 - 0.5 * (x - p80) / (p95 - p80)


def monotonic_score(x: float, *, low: float, high: float) -> float:
    """线性归一化到 [0,1]，x 低于 low 记 0，高于 high 记 1。"""
    if low >= high:
        raise ValueError("monotonic_score low must be below high")
    if x <= low:
        return 0.0
    if x >= high:
        return 1.0
    return (x - low) / (high - low)


def compute_score(feats: dict[str, float], config: dict) -> float:
    """计算加权质量分 S（0~1）。

    config 结构：{
        "dimensions": [{
            "feature": str,
            "weight": float,
            "mode": "inverted_u" | "monotonic",
            "p20"/"p40"/"p60"/"p80"/"p95" 或 "low"/"high": float,
        }, ...],
        "admission_threshold": float,   # 准入阈值（0~1），信号低于则拒绝
    }
    缺失/非有限特征按 0 分处理（该维度不贡献）。
    """
    total = 0.0
    for dim in config["dimensions"]:
        x = feats.get(dim["feature"])
        if x is None or not _isfinite(x):
            continue
        if dim["mode"] == "inverted_u":
            s = inverted_u_score(
                x,
                p20=dim["p20"],
                p40=dim["p40"],
                p60=dim["p60"],
                p80=dim["p80"],
                p95=dim["p95"],
            )
        elif dim["mode"] == "monotonic":
            s = monotonic_score(x, low=dim["low"], high=dim["high"])
        else:
            raise ValueError(f"unknown scoring mode: {dim['mode']}")
        total += dim["weight"] * s
    return min(total, 1.0)


def premium_pct(
    score: float,
    *,
    predicted_up_pct: float,
    mult: float,
    base_pct: float,
    cap_pct: float,
) -> float:
    """动态溢价% = base + S × 模型预测冲高% × mult，clip 到 cap（0=无上限）。"""
    raw = base_pct + score * predicted_up_pct * mult
    if cap_pct > 0 and raw > cap_pct:
        return float(cap_pct)
    return max(float(raw), base_pct)


def _isfinite(x: float) -> bool:
    try:
        import math

        return math.isfinite(x)
    except (TypeError, ValueError):
        return False