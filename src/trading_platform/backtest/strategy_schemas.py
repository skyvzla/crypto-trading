"""Versioned display descriptors for strategy-specific research fields."""

from __future__ import annotations

from typing import Any


SPIKE_SHORT_SCHEMA: dict[str, Any] = {
    "label": "Spike Short",
    "parameter_fields": [
        {"key": "strategy_version", "label": "策略版本", "format": "text"},
        {"key": "prior_high_lookback_hours", "label": "前高过滤周期", "format": "hours"},
        {"key": "rise_low_lookback_hours", "label": "上涨低点窗口", "format": "hours"},
        {"key": "min_rise_duration_hours", "label": "最短上涨周期", "format": "hours"},
        {"key": "entry_tier_mode", "label": "挂单档位模式", "format": "text"},
        {"key": "profit_unlock_percent", "label": "提前解除保护盈利", "format": "percent"},
        {"key": "total_notional", "label": "计划名义金额", "format": "usdt"},
        {"key": "exit_policy", "label": "退出策略", "format": "text"},
        {"key": "warmup_hours", "label": "预热时间", "format": "hours"},
    ],
    "detail_groups": [
        {
            "key": "signal",
            "label": "信号环境",
            "fields": [
                {"key": "entry_pattern", "label": "入场形态", "format": "text"},
                {"key": "spike_high", "label": "尖峰高点", "format": "price"},
                {"key": "prior_high", "label": "过滤前高", "format": "price"},
                {"key": "trigger_price", "label": "触发价格", "format": "price"},
                {"key": "rise_5s", "label": "5秒涨幅", "format": "percent"},
                {"key": "volume_multiple_5s", "label": "5秒量能倍数", "format": "number"},
            ],
        },
        {
            "key": "risk",
            "label": "执行与风险",
            "fields": [
                {"key": "invalid_price", "label": "失效价格", "format": "price"},
                {"key": "tier1_price", "label": "第一档", "format": "price"},
                {"key": "tier2_price", "label": "第二档", "format": "price"},
                {"key": "tier3_price", "label": "第三档", "format": "price"},
                {"key": "collision_status", "label": "交易竞争", "format": "text"},
            ],
        },
        {
            "key": "context",
            "label": "上涨与箱体",
            "fields": [
                {"key": "low_4h_age_hours", "label": "4小时低点年龄", "format": "hours"},
                {"key": "low_12h_age_hours", "label": "12小时低点年龄", "format": "hours"},
                {"key": "low_24h_age_hours", "label": "24小时低点年龄", "format": "hours"},
                {"key": "low_4h_3d_position", "label": "3天箱体位置", "format": "percent"},
                {"key": "low_4h_7d_position", "label": "7天箱体位置", "format": "percent"},
            ],
        },
    ],
    "chart_overlays": [
        {"key": "spike_high", "label": "尖峰高点", "kind": "price_line", "color": "#f59e0b", "line_style": "dashed"},
        {"key": "prior_high", "label": "过滤前高", "kind": "price_line", "color": "#eab308", "line_style": "dotted"},
        {"key": "tier1_price", "label": "第一档", "kind": "price_line", "color": "#38bdf8"},
        {"key": "tier2_price", "label": "第二档", "kind": "price_line", "color": "#22d3ee"},
        {"key": "tier3_price", "label": "第三档", "kind": "price_line", "color": "#2dd4bf"},
        {"key": "invalid_price", "label": "失效价", "kind": "price_line", "color": "#fb7185", "line_style": "dashed"},
    ],
}


def schema_for(strategy_id: str) -> dict[str, Any] | None:
    if strategy_id in {"spike_short", "spike-short"}:
        return {"strategy_id": strategy_id, "schema_version": 1, "descriptor": SPIKE_SHORT_SCHEMA}
    return None
