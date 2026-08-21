from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class FactorSpec:
    name: str
    group: str
    description: str
    scale_sensitive: bool = False


DEFAULT_FACTOR_SPECS = (
    FactorSpec("return_5s", "price", "5 秒价格涨幅"),
    FactorSpec("return_15s", "price", "15 秒价格涨幅"),
    FactorSpec("return_30s", "price", "30 秒价格涨幅"),
    FactorSpec("return_60s", "price", "60 秒价格涨幅"),
    FactorSpec("return_300s", "price", "5 分钟价格涨幅"),
    FactorSpec("price_velocity_5s", "price", "5 秒平均上涨速度"),
    FactorSpec("price_acceleration_5s", "price", "短期上涨加速度"),
    FactorSpec("upper_wick_ratio_1s", "structure", "当前 1s 上影线比例"),
    FactorSpec("volume_multiple_5s", "volume", "5 秒量相对前 60 秒中位量倍数"),
    FactorSpec("volume_zscore_5s", "volume", "5 秒均量相对前 60 秒 Z-score"),
    FactorSpec("quote_volume_zscore_5s", "volume", "USDT 成交额 5 秒 Z-score"),
    FactorSpec("taker_buy_ratio_1s", "orderflow", "当前秒主动买量占比"),
    FactorSpec("taker_buy_ratio_5s", "orderflow", "5 秒主动买量占比"),
    FactorSpec("taker_buy_ratio_60s", "orderflow", "60 秒主动买量占比"),
    FactorSpec("volume_imbalance_5s", "orderflow", "5 秒主动买卖量不平衡"),
    FactorSpec("volume_imbalance_60s", "orderflow", "60 秒主动买卖量不平衡"),
    FactorSpec(
        "orderflow_exhaustion_5s_vs_60s",
        "orderflow",
        "近期主动买占比相对 60 秒背景的衰竭程度",
    ),
    FactorSpec("quote_taker_buy_ratio_5s", "orderflow", "5 秒主动买入 USDT 金额占比"),
    FactorSpec("quote_volume_imbalance_5s", "orderflow", "5 秒 USDT 主动资金不平衡"),
    FactorSpec(
        "quote_orderflow_exhaustion_5s_vs_60s",
        "orderflow",
        "近期主动买入 USDT 占比相对 60 秒背景的衰竭",
    ),
    # 原始 CVD 保留供单 symbol 研究，但跨 symbol 比较时必须谨慎处理规模效应。
    FactorSpec("cvd_5s", "orderflow", "5 秒 CVD", scale_sensitive=True),
    FactorSpec("cvd_60s", "orderflow", "60 秒 CVD", scale_sensitive=True),
    FactorSpec("quote_cvd_5s", "orderflow", "5 秒 quote CVD", scale_sensitive=True),
    FactorSpec("oi_change_5m", "derivatives", "最近可见 5m OI 变化率"),
    FactorSpec("oi_change_15m", "derivatives", "最近可见 15m OI 变化率"),
    FactorSpec("oi_change_zscore_1h", "derivatives", "OI 变化的 1 小时异常程度"),
    FactorSpec(
        "count_long_short_ratio_zscore_24h",
        "derivatives",
        "全市场账户多空比 24h Z-score",
    ),
    FactorSpec(
        "sum_taker_long_short_vol_ratio_zscore_24h",
        "derivatives",
        "taker 多空成交比 24h Z-score",
    ),
    FactorSpec("price_oi_joint_5m", "derivatives", "5m 价格变化与 OI 变化联合强度"),
)


def available_factor_specs(
    dataset: pd.DataFrame,
    *,
    include_scale_sensitive: bool = False,
) -> tuple[FactorSpec, ...]:
    """返回当前 Dataset 实际可用且有非空值的第一批因子。"""
    selected: list[FactorSpec] = []
    for spec in DEFAULT_FACTOR_SPECS:
        if spec.name not in dataset.columns:
            continue
        if spec.scale_sensitive and not include_scale_sensitive:
            continue
        if pd.to_numeric(dataset[spec.name], errors="coerce").notna().sum() < 3:
            continue
        selected.append(spec)
    return tuple(selected)
