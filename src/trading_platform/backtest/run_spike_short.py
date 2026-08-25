#!/usr/bin/env python3
"""Dynamic Spike Short 策略专用回测入口。"""
import argparse
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from itertools import chain
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from trading_platform.backtest.engine import BacktestEngine, Event
from trading_platform.backtest.loader import BacktestDataLoader
from trading_platform.backtest.loader import DEFAULT_CHUNK_HOURS
from trading_platform.backtest.loader import MetricsDataLoader
from trading_platform.backtest.result import ResultAnalyzer
from trading_platform.backtest.runner import load_symbol_rules
from trading_platform.shared.config import BacktestConfig
from trading_platform.strategies.spike.capital import CapitalPolicyConfig
from trading_platform.strategies.spike.capital_replay import (
    CapitalManagedSpikeStrategy,
)
from trading_platform.strategies.spike.legacy_research import (
    LegacyScriptExitSpikeBacktestStrategy,
)
from trading_platform.strategies.spike.short import (
    DynamicSpikeBacktestStrategy,
    DynamicSpikeShortStrategy,
)
from trading_platform.strategies.spike.definition import (
    SpikeStrategyDefinition,
    load_strategy_definition,
)


DEFAULT_STRATEGY = "trading_platform.strategies.spike.v1:V1"


def no_prior_high_strategy_class(
    strategy_class: type[DynamicSpikeShortStrategy],
) -> type[DynamicSpikeShortStrategy]:
    """给任意 Spike 策略实现增加“禁用前高”的实验适配。"""

    class NoPriorHighStrategy(strategy_class):
        def __init__(self, *args, **kwargs):
            kwargs["prior_high_lookback_minutes"] = 1
            super().__init__(*args, **kwargs)
            self.prior_high_lookback_minutes = 0

        def _prior_high_point(self, minute_start: int):
            return Decimal("0"), minute_start

        def _detect_signal(self, bar):
            signal = super()._detect_signal(bar)
            if signal is not None:
                signal.prior_high = None
                signal.prior_high_time = None
            return signal

    NoPriorHighStrategy.__name__ = f"NoPriorHigh{strategy_class.__name__}"
    return NoPriorHighStrategy


@dataclass(frozen=True)
class SpikeBacktestSettings:
    strategy_path: str
    strategy_version: str
    strategy_definition: SpikeStrategyDefinition
    start_ms: int
    end_ms: int
    load_start_ms: int
    bar1s_time_shift_ms: int
    prior_high_lookback_minutes: int
    rise_low_lookback_minutes: int
    min_rise_duration_minutes: int
    box_duration_min_minutes: int
    spike_avg_deviation_max_pct: float
    spike_range_max_pct: float
    spike_vwap_deviation_max_pct: float
    entry_tier_mode: str
    capital_config: CapitalPolicyConfig | None
    reject_below_current: bool
    entry_premium_mult: float
    entry_premium_floor: float
    entry_premium_cap: float
    entry_premium_model: str | None
    entry_scoring_enabled: bool
    entry_scoring_threshold: float
    entry_scoring_config: str | None
    entry_premium_base_pct: float
    oi_stop_enabled: bool
    oi_stop_oi_rise_pct: float
    oi_stop_loss_pct: float
    early_profit_unlock_ratio: Decimal | None
    max_consecutive_up_minutes: int
    group_rise_12h_threshold: float
    loose_consecutive_up_minutes: int
    loose_max_ls_ratio: float | None
    strong_tier_atr_shift: float
    exit_strict_age_ms: int
    exit_flat_agreement: int | None
    time_risk_grace_ms: int
    time_risk_grace_loss_ratio: float
    strong_strict_age_ms: int | None
    weak_strict_age_ms: int | None
    strong_bucket_strict_age_ms: int | None
    weak_bucket_strict_age_ms: int | None
    profit_unlock_ratio: Decimal | None
    profit_drawdown_ratio: Decimal | None
    profit_drawdown_peak_ratio: Decimal | None
    max_oi_change_pct: float
    max_ls_ratio: float
    rise_5s_threshold: Decimal
    max_rise_5s_percent: Decimal | None
    max_rise_window_seconds: int
    max_rise_window_percent: Decimal | None
    accel_rise_5s_min: Decimal
    accel_ratio: Decimal
    accel_prev_minutes: int
    max_volume_multiple_5s: Decimal | None
    min_td_sell_setup_5m: int
    min_volume_multiple_5m: Decimal
    prior_high_tolerance_percent: Decimal
    required_kline_intervals: tuple[str, ...]
    requires_bar1s: bool
    execution_timeframe: str
    duckdb_path: str
    output_path: Path


def load_metrics_series(
    metrics_root: str | Path,
    symbol: str,
) -> list[tuple[int, float, float]]:
    """从 metrics parquet 归档加载单币 5m 指标序列。

    返回按策略可见时间升序的 [(available_ms, oi, ls_ratio)]；归档缺失或为空时返回空列表。
    """
    try:
        return MetricsDataLoader(metrics_root, symbol=symbol).load()
    except (FileNotFoundError, RuntimeError, ValueError):
        return []


def _timestamp_ms(value: str) -> int:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Dynamic Spike Short Strategy Backtest"
    )
    parser.add_argument("--symbol", default="AKEUSDT", help="Trading symbol")
    parser.add_argument("--start", required=True, help="Start time in ISO format")
    parser.add_argument("--end", required=True, help="End time in ISO format")
    parser.add_argument(
        "--total-notional",
        type=Decimal,
        default=None,
        help="旧固定资金模式的每轮名义金额；动态资金模式不需要",
    )
    parser.add_argument(
        "--initial-account-capital",
        type=Decimal,
        default=None,
        help="动态资金池的初始账户资金；与 --initial-trading-capital 同时提供",
    )
    parser.add_argument(
        "--initial-trading-capital",
        type=Decimal,
        default=None,
        help="动态资金池的初始可交易资金",
    )
    parser.add_argument(
        "--profit-reinvest-ratio",
        type=Decimal,
        default=Decimal("0.5"),
        help="盈利进入可交易资金池的比例（0..1）",
    )
    parser.add_argument(
        "--minimum-trading-capital",
        type=Decimal,
        default=Decimal("0"),
        help="可交易资金低于或等于该值时停止新开仓",
    )
    parser.add_argument(
        "--duckdb-path",
        required=True,
        help="只读 DuckDB candles 归档路径",
    )
    parser.add_argument(
        "--output",
        default="reports/spike_short_backtest",
        help="Output directory",
    )
    parser.add_argument(
        "--warmup-hours",
        type=float,
        default=16.0,
        help="Indicator warmup period before --start (default: 16)",
    )
    parser.add_argument(
        "--exit-policy",
        choices=("confirmed", "candidate-v1", "legacy-script"),
        default=None,
        help="退出策略；未传时使用策略声明中的默认值",
    )
    parser.add_argument(
        "--limit-fill-fraction",
        type=float,
        default=1.0,
        help="每根穿价 1s Bar 最多成交 LIMIT 原数量的比例（0, 1]",
    )
    parser.add_argument(
        "--bar1s-time-shift-hours",
        type=Decimal,
        default=Decimal("0"),
        help="显式修正历史 1s 数据时间偏移；默认 0，不自动推断",
    )
    parser.add_argument(
        "--research",
        action="store_true",
        default=False,
        help="研究模式：三高/atr 过滤 + 动态溢价挂单 + 止损 + 4h 持有",
    )
    parser.add_argument("--th-v", type=float, default=0.0, help="研究：vwap_dev_5m 过滤阈值")
    parser.add_argument("--th-e", type=float, default=0.0, help="研究：ema_ratio_5m 过滤阈值")
    parser.add_argument("--th-r", type=float, default=0.0, help="研究：roc_5m 过滤阈值")
    parser.add_argument("--atr-min", type=float, default=0.0, help="研究：atr_ratio_5m 过滤阈值")
    parser.add_argument("--premium-mult", type=float, default=0.7, help="研究：预测冲高溢价倍数")
    parser.add_argument("--premium-floor", type=float, default=3.0, help="研究：溢价下限(%)")
    parser.add_argument("--premium-cap", type=float, default=35.0, help="研究：溢价上限(%)")
    parser.add_argument("--stop-pct", type=float, default=8.0, help="研究：止损幅度(%)")
    parser.add_argument("--stop-check-ms", type=int, default=900000, help="研究：止损检查时点(ms)，0=入场后逐bar")
    parser.add_argument("--model-json", default=None, help="研究：模型权重 JSON（默认 research/up_premium_model.py）")
    parser.add_argument(
        "--strategy",
        default=DEFAULT_STRATEGY,
        help="策略声明路径，格式为 module:attribute",
    )
    parser.add_argument(
        "--prior-high-lookback-hours",
        type=int,
        default=None,
        help="前高过滤回看周期（小时），0 表示禁用；默认由策略声明决定",
    )
    parser.add_argument(
        "--rise-low-lookback-hours",
        type=int,
        default=None,
        help="上涨起点最低价的回看窗口（小时）；默认由策略声明决定",
    )
    parser.add_argument(
        "--min-rise-duration-hours",
        type=int,
        default=None,
        help="窗口最低点距信号的最短小时数；默认由策略声明决定",
    )
    # ---- Spike v3（pullback-v3）入场参数（默认 None=策略声明/类默认值）----
    parser.add_argument(
        "--rise-3s-threshold",
        type=float,
        default=None,
        help="v3：3 秒暴涨涨幅门槛（小数），默认 0.03",
    )
    parser.add_argument(
        "--vol-multiple",
        type=float,
        default=None,
        help="v3：3 秒成交量相对 60s 中位数倍数门槛，默认 2.0",
    )
    parser.add_argument(
        "--cooldown-seconds",
        type=int,
        default=None,
        help="v3：3s 信号冷却秒数，默认 180",
    )
    parser.add_argument(
        "--min-spike-rise",
        type=float,
        default=None,
        help="v3：插针总涨幅门槛（spike_high/origin-1），默认 0.30",
    )
    parser.add_argument(
        "--retrace-frac",
        type=float,
        default=None,
        help="v3：回吐插针涨幅比例后接空，默认 0.35",
    )
    parser.add_argument(
        "--buy-ratio-entry-min",
        type=float,
        default=None,
        help="v3：接空前 10s 主动买占比轻过滤下限，默认 0.40",
    )
    parser.add_argument(
        "--no-stop-5m-high",
        action="store_true",
        default=False,
        help="v3：关闭 5m 插针高点止损（默认开启）",
    )
    parser.add_argument(
        "--take-profit",
        type=float,
        default=None,
        help="v3：硬止盈比例（小数），默认 0.10",
    )
    parser.add_argument(
        "--max-hold-seconds",
        type=int,
        default=None,
        help="v3：持仓超时秒数，默认 3600",
    )
    parser.add_argument(
        "--wait-seconds",
        type=int,
        default=None,
        help="v3：信号后等待回落的最大秒数，默认 3600",
    )
    parser.add_argument(
        "--entry-tier-mode",
        choices=("three-tier", "tier3-only", "single-entry"),
        default=None,
        help="入场挂单模式；默认由策略声明决定",
    )
    parser.add_argument(
        "--reject-below-current",
        action="store_true",
        default=False,
        help="现价已高于挂单档位时拒绝信号（低卖防护）；默认关闭",
    )
    parser.add_argument(
        "--entry-premium-mult",
        type=float,
        default=0.0,
        help="动态溢价挂单倍数（0=关闭，回退默认 spike_high−ATR 三档）",
    )
    parser.add_argument(
        "--entry-premium-floor",
        type=float,
        default=3.0,
        help="动态溢价挂单下限（%），仅 entry-premium-mult>0 时生效",
    )
    parser.add_argument(
        "--entry-premium-cap",
        type=float,
        default=35.0,
        help="动态溢价挂单上限（%），仅 entry-premium-mult>0 时生效",
    )
    parser.add_argument(
        "--entry-premium-model",
        default=None,
        help="动态溢价模型权重 JSON 路径；默认使用 research/up_premium_model.py 内置权重",
    )
    parser.add_argument(
        "--entry-scoring-enabled",
        action="store_true",
        default=False,
        help="启用评分准入（低于阈值拒绝信号）",
    )
    parser.add_argument(
        "--entry-scoring-threshold",
        type=float,
        default=0.5,
        help="评分准入阈值（0~1）",
    )
    parser.add_argument(
        "--entry-scoring-config",
        default=None,
        help="评分配置 JSON 路径（维度+权重+边界+准入阈值）",
    )
    parser.add_argument(
        "--entry-premium-base-pct",
        type=float,
        default=1.0,
        help="动态溢价基础%（S=0 时的最低溢价）",
    )
    parser.add_argument(
        "--oi-stop-enabled",
        action="store_true",
        default=False,
        help="启用 OI 止损：插针后首个有效 5m OI 点升幅超阈值且浮亏达标时平仓",
    )
    parser.add_argument(
        "--oi-stop-oi-rise-pct",
        type=float,
        default=5.0,
        help="OI 止损：确认 OI 点相对基准点升幅阈值（%）",
    )
    parser.add_argument(
        "--oi-stop-loss-pct",
        type=float,
        default=3.0,
        help="OI 止损：确认点时刻浮亏阈值（%）",
    )
    parser.add_argument(
        "--box-duration-min-hours",
        type=int,
        default=None,
        help="现价站上箱体/通道突破线（3d/7d 上沿均值）的最小小时数；"
        "0/None 表示关闭该过滤",
    )
    parser.add_argument(
        "--spike-avg-deviation-max-pct",
        type=float,
        default=0.0,
        help="过早触发过滤：信号触发价相对前 30m 均价的偏离度阈值（%）；"
        "0 关闭（需与 --spike-range-max-pct 同时设置）",
    )
    parser.add_argument(
        "--spike-range-max-pct",
        type=float,
        default=0.0,
        help="过早触发过滤：信号前 60m 价格极差阈值（%）；"
        "0 关闭（需与 --spike-avg-deviation-max-pct 同时设置）",
    )
    parser.add_argument(
        "--spike-vwap-deviation-max-pct",
        type=float,
        default=0.0,
        help="VWAP 偏离过滤：信号触发价相对前 100m 聚合 20 根 5m VWAP "
        "偏离阈值（%）；0 关闭",
    )
    parser.add_argument(
        "--profit-unlock-percent",
        type=Decimal,
        default=None,
        help="持仓价格盈利超过该百分比后永久解除前90秒风险保护",
    )
    parser.add_argument(
        "--max-consecutive-up-minutes",
        type=int,
        default=0,
        help="信号前连续上涨1m K线根数上限；0 表示不限制",
    )
    parser.add_argument(
        "--group-rise-12h-threshold",
        type=float,
        default=0.0,
        help="按动能分组的 12h 涨幅阈值（小数）；0 关闭分组，仅用于离线研究",
    )
    parser.add_argument(
        "--loose-consecutive-up-minutes",
        type=int,
        default=0,
        help="弱势/蓄力桶连阳上限；group 开启且 12h 涨幅低于阈值时生效",
    )
    parser.add_argument(
        "--loose-max-ls-ratio",
        type=float,
        default=None,
        help="弱势/蓄力桶多空比上限；None 不放宽，0 表示弱势桶 LS 不限制",
    )
    parser.add_argument(
        "--strong-tier-atr-shift",
        type=float,
        default=0.0,
        help="强势桶三档挂单价 ATR 系数偏移；正数上移挂单价，0 不调整",
    )
    parser.add_argument(
        "--exit-strict-age-ms",
        type=int,
        default=900000,
        help="candidate-v1 时间止损阈值（ms）；单档模式同时作为风险启动时刻",
    )
    parser.add_argument(
        "--exit-flat-agreement",
        type=int,
        default=None,
        help="candidate-v1 单档动量一致要求（1-3）；None 保留默认 3/2/1 分档",
    )
    parser.add_argument(
        "--time-risk-grace-ms",
        type=int,
        default=0,
        help="candidate-v1 时间止损宽限期（ms）：浮亏低于阈值时延后止损；0 关闭",
    )
    parser.add_argument(
        "--time-risk-grace-loss-ratio",
        type=float,
        default=0.01,
        help="宽限期内相对名义本金的浮亏阈值（0-1）；浮亏超过该值立即止损",
    )
    parser.add_argument(
        "--strong-strict-age-ms",
        type=int,
        default=None,
        help="动量一致（decay_agreement>=1）时的 time_risk 止损时间（ms）；None 使用 exit-strict-age-ms",
    )
    parser.add_argument(
        "--weak-strict-age-ms",
        type=int,
        default=None,
        help="动量衰竭（decay_agreement<1）时的 time_risk 止损时间（ms）；None 使用 exit-strict-age-ms",
    )
    parser.add_argument(
        "--strong-bucket-strict-age-ms",
        type=int,
        default=None,
        help="静态强桶（入场 rise_from_12h_low>=group_rise_12h_threshold）的 time_risk 止损时间（ms）；None 关闭静态分档",
    )
    parser.add_argument(
        "--weak-bucket-strict-age-ms",
        type=int,
        default=None,
        help="静态弱桶（入场 rise_from_12h_low<group_rise_12h_threshold）的 time_risk 止损时间（ms）；None 关闭静态分档",
    )
    parser.add_argument(
        "--profit-unlock-ratio",
        type=float,
        default=None,
        help="浮盈解锁比例（0-1）：持仓浮盈相对名义本金达到该比例后，动量分档时间直接降到最低（忽略时间限制）；None 关闭",
    )
    parser.add_argument(
        "--profit-drawdown-ratio",
        type=float,
        default=None,
        help="浮盈回撤保护（0-1）：解锁后价格从持仓峰值回撤达到该比例立即止盈退出；None 关闭",
    )
    parser.add_argument(
        "--profit-drawdown-peak-ratio",
        type=float,
        default=None,
        help="回撤保护浮盈前置（0-1）：峰值浮盈达到该比例后才启用回撤保护（粘滞，不与弱化时间耦合）；None 回退到 unlock 语义",
    )
    parser.add_argument(
        "--max-oi-change-pct",
        type=float,
        default=0.0,
        help="信号时刻 OI 相对上一 5m 快照的变化上限（%）；0 表示不限制",
    )
    parser.add_argument(
        "--max-ls-ratio",
        type=float,
        default=0.0,
        help="信号时刻全市场多空比上限；0 表示不限制",
    )
    parser.add_argument(
        "--rise-5s-threshold-percent", type=Decimal, default=None,
        help="5秒涨幅触发阈值（百分比）；默认使用策略声明值",
    )
    parser.add_argument(
        "--accel-rise-5s-min-percent", type=Decimal, default=None,
        help="加速豁免的最低 5 秒涨幅（百分比）；默认 0 表示关闭豁免",
    )
    parser.add_argument(
        "--accel-ratio", type=Decimal, default=None,
        help="加速豁免倍率：当前窗口平均每秒涨幅 / 前窗口；默认 2.0",
    )
    parser.add_argument(
        "--accel-prev-minutes", type=int, default=None,
        help="加速豁免前窗口分钟数；默认 10",
    )
    parser.add_argument(
        "--max-rise-5s-percent", type=Decimal, default=None,
        help="兼容旧实验的 5 秒涨幅上限（百分比）；不可与通用窗口上限同时传递",
    )
    parser.add_argument(
        "--max-rise-window-seconds", type=int, default=5,
        help="顶部急涨上限的观测窗口（秒，1-60）；默认 5，不改变基础入场触发",
    )
    parser.add_argument(
        "--max-rise-window-percent", type=Decimal, default=None,
        help="顶部急涨上限（百分比）；0 或不传表示不限制",
    )
    parser.add_argument(
        "--max-volume-multiple-5s", type=Decimal, default=None,
        help="5秒成交量相对基准倍数上限；0 或不传表示不限制",
    )
    parser.add_argument(
        "--min-td-sell-setup-5m", type=int, default=0,
        help="入场前已完成 5m TD 卖出 setup 最小值；0 表示不限制",
    )
    parser.add_argument(
        "--min-volume-multiple-5m", type=Decimal, default=Decimal("0"),
        help="入场前已完成 5m K线相对量能最小倍数；0 表示不限制",
    )
    parser.add_argument(
        "--prior-high-tolerance-percent", type=Decimal, default=None,
        help="允许最低挂单价低于前高的百分比；0表示严格高于前高",
    )
    parser.add_argument(
        "--exchange-info",
        type=Path,
        default=None,
        help="可选 Binance exchangeInfo JSON 快照，用于 tick/step 量化",
    )
    parser.add_argument(
        "--chunk-hours",
        type=float,
        default=DEFAULT_CHUNK_HOURS,
        help="DuckDB 流式回测的时间窗口（小时，默认 4320 小时/180 天）",
    )
    parser.add_argument(
        "--fetch-batch-size",
        type=int,
        default=10_000,
        help="每次从 DuckDB 取出的事件行数",
    )
    parser.add_argument(
        "--duckdb-memory-limit",
        default=None,
        help="单个 DuckDB worker 的内存上限，例如 1GB",
    )
    parser.add_argument(
        "--duckdb-threads",
        type=int,
        default=1,
        help="单个 DuckDB worker 使用的线程数",
    )
    parser.add_argument(
        "--archive-index",
        type=Path,
        default=None,
        help="归档 sidecar 索引；参数矩阵回测用它跳过重复全区间扫描",
    )
    parser.add_argument(
        "--metrics-root",
        type=Path,
        default=None,
        help="可选 5m OI/多空比 metrics 归档根目录；启用 --max-oi-change-pct/--max-ls-ratio 时需要",
    )
    return parser.parse_args(argv)


def resolve_settings(args: argparse.Namespace) -> SpikeBacktestSettings:
    if args.total_notional is not None and args.total_notional <= 0:
        raise ValueError("--total-notional must be positive")
    start_ms = _timestamp_ms(args.start)
    end_ms = _timestamp_ms(args.end)
    if start_ms >= end_ms:
        raise ValueError("--start must be earlier than --end")
    if args.warmup_hours < 0:
        raise ValueError("--warmup-hours must not be negative")
    definition = load_strategy_definition(args.strategy)
    defaults = definition.defaults
    if args.exit_policy is None:
        args.exit_policy = defaults.exit_policy
    prior_high_lookback_hours = args.prior_high_lookback_hours
    if prior_high_lookback_hours is None:
        prior_high_lookback_hours = defaults.prior_high_lookback_hours
    rise_low_lookback_hours = args.rise_low_lookback_hours
    if rise_low_lookback_hours is None:
        rise_low_lookback_hours = defaults.rise_low_lookback_hours
    min_rise_duration_hours = args.min_rise_duration_hours
    if min_rise_duration_hours is None:
        min_rise_duration_hours = defaults.min_rise_duration_hours
    entry_tier_mode = args.entry_tier_mode or defaults.entry_tier_mode
    initial_capitals = (
        args.initial_account_capital,
        args.initial_trading_capital,
    )
    if (initial_capitals[0] is None) != (initial_capitals[1] is None):
        raise ValueError(
            "--initial-account-capital and --initial-trading-capital "
            "must be provided together"
        )
    capital_config = None
    if initial_capitals[0] is not None:
        capital_config = CapitalPolicyConfig(
            initial_account_capital=initial_capitals[0],
            initial_trading_capital=initial_capitals[1],
            profit_reinvest_ratio=args.profit_reinvest_ratio,
            minimum_trading_capital=args.minimum_trading_capital,
        )
    if capital_config is None and args.total_notional is None:
        raise ValueError(
            "--total-notional or both dynamic capital values are required"
        )
    if entry_tier_mode == "single-entry" and capital_config is None:
        raise ValueError(
            "single-entry requires --initial-account-capital and "
            "--initial-trading-capital"
        )
    if capital_config is not None and (
        args.research or args.exit_policy == "legacy-script"
    ):
        raise ValueError("dynamic capital is only supported by the active Spike strategy")
    reject_below_current = bool(args.reject_below_current)
    box_duration_min_hours = args.box_duration_min_hours
    if box_duration_min_hours is None:
        box_duration_min_hours = getattr(defaults, "box_duration_min_hours", 0)
    rise_5s_threshold = (
        Decimal(str(args.rise_5s_threshold_percent)) / Decimal("100")
        if args.rise_5s_threshold_percent is not None else Decimal("0.05")
    )
    accel_rise_5s_min = (
        Decimal(str(args.accel_rise_5s_min_percent)) / Decimal("100")
        if args.accel_rise_5s_min_percent is not None else Decimal("0")
    )
    if accel_rise_5s_min < 0:
        raise ValueError("--accel-rise-5s-min-percent must not be negative")
    if accel_rise_5s_min > rise_5s_threshold:
        raise ValueError(
            "--accel-rise-5s-min-percent must not exceed rise-5s-threshold-percent"
        )
    accel_ratio = (
        args.accel_ratio if args.accel_ratio is not None else Decimal("2")
    )
    if accel_ratio <= 1:
        raise ValueError("--accel-ratio must be greater than 1")
    accel_prev_minutes = args.accel_prev_minutes or 10
    if accel_prev_minutes < 1:
        raise ValueError("--accel-prev-minutes must be positive")
    prior_high_tolerance_percent = (
        args.prior_high_tolerance_percent
        if args.prior_high_tolerance_percent is not None else Decimal("0")
    )
    max_rise_5s_percent = args.max_rise_5s_percent
    max_rise_window_percent = args.max_rise_window_percent
    max_volume_multiple_5s = args.max_volume_multiple_5s
    min_td_sell_setup_5m = args.min_td_sell_setup_5m
    min_volume_multiple_5m = args.min_volume_multiple_5m
    if max_rise_5s_percent is not None and max_rise_5s_percent < 0:
        raise ValueError("--max-rise-5s-percent must not be negative")
    if max_rise_window_percent is not None and max_rise_window_percent < 0:
        raise ValueError("--max-rise-window-percent must not be negative")
    if not 1 <= args.max_rise_window_seconds <= 60:
        raise ValueError("--max-rise-window-seconds must be between 1 and 60")
    if max_rise_5s_percent is not None and max_rise_window_percent is not None:
        raise ValueError(
            "--max-rise-5s-percent and --max-rise-window-percent "
            "cannot both be set"
        )
    if max_volume_multiple_5s is not None and max_volume_multiple_5s < 0:
        raise ValueError("--max-volume-multiple-5s must not be negative")
    if not 0 <= min_td_sell_setup_5m <= 9:
        raise ValueError("--min-td-sell-setup-5m must be between 0 and 9")
    if min_volume_multiple_5m < 0:
        raise ValueError("--min-volume-multiple-5m must not be negative")
    if max_rise_5s_percent == 0:
        max_rise_5s_percent = None
    if max_rise_window_percent == 0:
        max_rise_window_percent = None
    if max_volume_multiple_5s == 0:
        max_volume_multiple_5s = None
    if (
        max_volume_multiple_5s is not None
        and max_volume_multiple_5s
        < DynamicSpikeShortStrategy.VOLUME_MULTIPLE_5S
    ):
        raise ValueError(
            "--max-volume-multiple-5s must be zero or at least the lower volume threshold"
        )
    if (
        (max_rise_window_percent or max_rise_5s_percent) is not None
        and (max_rise_window_percent or max_rise_5s_percent)
        < rise_5s_threshold * Decimal("100")
    ):
        raise ValueError(
            "maximum rise must be greater than or equal to the lower threshold"
        )
    profit_unlock_percent = args.profit_unlock_percent
    if profit_unlock_percent is None and defaults.profit_unlock_percent is not None:
        profit_unlock_percent = Decimal(str(defaults.profit_unlock_percent))

    optional_parameters = {
        "max_consecutive_up_minutes": args.max_consecutive_up_minutes,
        "max_oi_change_pct": args.max_oi_change_pct,
        "max_ls_ratio": args.max_ls_ratio,
        "rise_5s_threshold_percent": args.rise_5s_threshold_percent,
        "accel_rise_5s_min": args.accel_rise_5s_min_percent,
        "accel_ratio": args.accel_ratio,
        "accel_prev_minutes": args.accel_prev_minutes,
        "max_rise_5s_percent": args.max_rise_5s_percent,
        "max_rise_window_percent": args.max_rise_window_percent,
        "max_rise_window_seconds": (
            args.max_rise_window_seconds
            if args.max_rise_window_percent is not None
            else None
        ),
        "max_volume_multiple_5s": args.max_volume_multiple_5s,
        "min_td_sell_setup_5m": min_td_sell_setup_5m,
        "min_volume_multiple_5m": min_volume_multiple_5m,
        "prior_high_tolerance_percent": args.prior_high_tolerance_percent,
        "group_rise_12h_threshold": args.group_rise_12h_threshold,
        "loose_consecutive_up_minutes": args.loose_consecutive_up_minutes,
        "loose_max_ls_ratio": args.loose_max_ls_ratio,
        "strong_tier_atr_shift": args.strong_tier_atr_shift,
        "exit_strict_age_ms": (
            args.exit_strict_age_ms
            if 0 < args.exit_strict_age_ms != 900000
            else None
        ),
        "exit_flat_agreement": (
            args.exit_flat_agreement if args.exit_flat_agreement else None
        ),
        "time_risk_grace_ms": args.time_risk_grace_ms,
        "time_risk_grace_loss_ratio": (
            args.time_risk_grace_loss_ratio
            if args.time_risk_grace_loss_ratio != 0.01
            else None
        ),
        "strong_strict_age_ms": args.strong_strict_age_ms,
        "weak_strict_age_ms": args.weak_strict_age_ms,
        "strong_bucket_strict_age_ms": args.strong_bucket_strict_age_ms,
        "weak_bucket_strict_age_ms": args.weak_bucket_strict_age_ms,
        "profit_unlock_ratio": args.profit_unlock_ratio,
        "profit_drawdown_ratio": args.profit_drawdown_ratio,
        "profit_drawdown_peak_ratio": args.profit_drawdown_peak_ratio,
    }
    unsupported = sorted(
        key
        for key, value in optional_parameters.items()
        if value and key not in definition.supported_parameters
    )
    if unsupported:
        raise ValueError(
            f"strategy {definition.name} does not support: {', '.join(unsupported)}"
        )
    if definition.data_requirements.metrics_5m and args.metrics_root is None:
        raise ValueError(f"strategy {definition.name} requires --metrics-root")
    if args.exit_policy == "legacy-script" and (
        max_rise_5s_percent is not None
        or max_rise_window_percent is not None
        or max_volume_multiple_5s is not None
    ):
        raise ValueError(
            "rise/volume upper limits "
            "are not supported with --exit-policy legacy-script"
        )

    if prior_high_lookback_hours < 0:
        raise ValueError("--prior-high-lookback-hours must not be negative")
    if rise_low_lookback_hours < 0 or min_rise_duration_hours < 0:
        raise ValueError("rise lookback and minimum duration must not be negative")
    if (rise_low_lookback_hours == 0) != (min_rise_duration_hours == 0):
        raise ValueError("rise lookback and minimum duration must both be zero or positive")
    if min_rise_duration_hours > rise_low_lookback_hours:
        raise ValueError("minimum rise duration must not exceed rise lookback")
    if box_duration_min_hours < 0:
        raise ValueError("--box-duration-min-hours must not be negative")
    spike_avg_deviation_max_pct = args.spike_avg_deviation_max_pct
    spike_range_max_pct = args.spike_range_max_pct
    spike_vwap_deviation_max_pct = args.spike_vwap_deviation_max_pct
    if spike_vwap_deviation_max_pct < 0:
        raise ValueError("--spike-vwap-deviation-max-pct must not be negative")
    if spike_avg_deviation_max_pct < 0 or spike_range_max_pct < 0:
        raise ValueError(
            "--spike-avg-deviation-max-pct and --spike-range-max-pct "
            "must not be negative"
        )
    if (spike_avg_deviation_max_pct > 0) != (spike_range_max_pct > 0):
        raise ValueError(
            "--spike-avg-deviation-max-pct and --spike-range-max-pct "
            "must both be zero or both be positive"
        )
    if (
        profit_unlock_percent is not None
        and not Decimal("0") < profit_unlock_percent < Decimal("100")
    ):
        raise ValueError("--profit-unlock-percent must be between 0 and 100")
    if profit_unlock_percent is not None and args.exit_policy != "candidate-v1":
        raise ValueError("--profit-unlock-percent requires --exit-policy candidate-v1")
    warmup_hours = max(
        args.warmup_hours,
        float(prior_high_lookback_hours),
        float(rise_low_lookback_hours),
        float(box_duration_min_hours + 7 * 24) if box_duration_min_hours else 0.0,
    )
    bar1s_time_shift_ms = int(
        args.bar1s_time_shift_hours * Decimal("3600000")
    )
    return SpikeBacktestSettings(
        strategy_path=args.strategy,
        strategy_version=definition.name,
        strategy_definition=definition,
        start_ms=start_ms,
        end_ms=end_ms,
        load_start_ms=start_ms - int(warmup_hours * 3_600_000),
        bar1s_time_shift_ms=bar1s_time_shift_ms,
        prior_high_lookback_minutes=prior_high_lookback_hours * 60,
        rise_low_lookback_minutes=rise_low_lookback_hours * 60,
        min_rise_duration_minutes=min_rise_duration_hours * 60,
        box_duration_min_minutes=box_duration_min_hours * 60,
        spike_avg_deviation_max_pct=spike_avg_deviation_max_pct,
        spike_range_max_pct=spike_range_max_pct,
        spike_vwap_deviation_max_pct=spike_vwap_deviation_max_pct,
        entry_tier_mode=entry_tier_mode,
        capital_config=capital_config,
        reject_below_current=reject_below_current,
        entry_premium_mult=args.entry_premium_mult,
        entry_premium_floor=args.entry_premium_floor,
        entry_premium_cap=args.entry_premium_cap,
        entry_premium_model=args.entry_premium_model,
        entry_scoring_enabled=args.entry_scoring_enabled,
        entry_scoring_threshold=args.entry_scoring_threshold,
        entry_scoring_config=args.entry_scoring_config,
        entry_premium_base_pct=args.entry_premium_base_pct,
        oi_stop_enabled=args.oi_stop_enabled,
        oi_stop_oi_rise_pct=args.oi_stop_oi_rise_pct,
        oi_stop_loss_pct=args.oi_stop_loss_pct,
        early_profit_unlock_ratio=(
            profit_unlock_percent / Decimal("100")
            if profit_unlock_percent is not None
            else None
        ),
        max_consecutive_up_minutes=args.max_consecutive_up_minutes,
        group_rise_12h_threshold=args.group_rise_12h_threshold,
        loose_consecutive_up_minutes=args.loose_consecutive_up_minutes,
        loose_max_ls_ratio=args.loose_max_ls_ratio,
        strong_tier_atr_shift=args.strong_tier_atr_shift,
        exit_strict_age_ms=(
            args.exit_strict_age_ms if args.exit_strict_age_ms > 0 else 900000
        ),
        exit_flat_agreement=(
            args.exit_flat_agreement if args.exit_flat_agreement else None
        ),
        time_risk_grace_ms=args.time_risk_grace_ms,
        time_risk_grace_loss_ratio=Decimal(str(args.time_risk_grace_loss_ratio)),
        strong_strict_age_ms=(
            args.strong_strict_age_ms if args.strong_strict_age_ms else None
        ),
        weak_strict_age_ms=(
            args.weak_strict_age_ms if args.weak_strict_age_ms else None
        ),
        strong_bucket_strict_age_ms=(
            args.strong_bucket_strict_age_ms
            if args.strong_bucket_strict_age_ms
            else None
        ),
        weak_bucket_strict_age_ms=(
            args.weak_bucket_strict_age_ms if args.weak_bucket_strict_age_ms else None
        ),
        profit_unlock_ratio=(
            Decimal(str(args.profit_unlock_ratio))
            if args.profit_unlock_ratio
            else None
        ),
        profit_drawdown_ratio=(
            Decimal(str(args.profit_drawdown_ratio))
            if args.profit_drawdown_ratio
            else None
        ),
        profit_drawdown_peak_ratio=(
            Decimal(str(args.profit_drawdown_peak_ratio))
            if args.profit_drawdown_peak_ratio
            else None
        ),
        max_oi_change_pct=args.max_oi_change_pct,
        max_ls_ratio=args.max_ls_ratio,
        rise_5s_threshold=rise_5s_threshold,
        max_rise_5s_percent=max_rise_5s_percent,
        max_rise_window_seconds=args.max_rise_window_seconds,
        max_rise_window_percent=max_rise_window_percent,
        accel_rise_5s_min=accel_rise_5s_min,
        accel_ratio=accel_ratio,
        accel_prev_minutes=accel_prev_minutes,
        max_volume_multiple_5s=max_volume_multiple_5s,
        min_td_sell_setup_5m=min_td_sell_setup_5m,
        min_volume_multiple_5m=min_volume_multiple_5m,
        prior_high_tolerance_percent=prior_high_tolerance_percent,
        required_kline_intervals=tuple(
            dict.fromkeys(
                timeframe
                for timeframe in definition.data_requirements.market_timeframes
                if timeframe != "1s"
            )
        )
        + (
            ("15m",)
            if args.exit_policy == "candidate-v1"
            and "15m" not in definition.data_requirements.market_timeframes
            else ()
        ),
        requires_bar1s="1s" in definition.data_requirements.market_timeframes,
        execution_timeframe=definition.data_requirements.execution_timeframe,
        duckdb_path=args.duckdb_path,
        output_path=Path(args.output),
    )


def create_spike_engine(
    args: argparse.Namespace,
    settings: SpikeBacktestSettings,
    events: Iterable[Event],
    *,
    preloaded_metrics_series: list[tuple[int, float, float]] | None = None,
) -> BacktestEngine:
    config = BacktestConfig(
        data_dir=settings.duckdb_path,
        output_dir=str(settings.output_path),
        trading_start_ms=settings.start_ms,
        limit_fill_fraction_per_bar=args.limit_fill_fraction,
        bar1s_time_shift_ms=settings.bar1s_time_shift_ms,
        prior_high_lookback_minutes=(
            settings.prior_high_lookback_minutes or 1
        ),
        strategy_path=settings.strategy_path,
        spike_strategy_version=settings.strategy_version,
        spike_entry_tier_mode=settings.entry_tier_mode,
        spike_rise_low_lookback_minutes=settings.rise_low_lookback_minutes,
        spike_min_rise_duration_minutes=settings.min_rise_duration_minutes,
        spike_early_profit_unlock_ratio=(
            float(settings.early_profit_unlock_ratio)
            if settings.early_profit_unlock_ratio is not None
            else None
        ),
    )
    if settings.prior_high_lookback_minutes == 0:
        config.prior_high_lookback_minutes = 0
    metrics_by_symbol = None
    if settings.strategy_definition.data_requirements.metrics_5m:
        series = (
            preloaded_metrics_series
            if preloaded_metrics_series is not None
            else load_metrics_series(args.metrics_root, args.symbol)
        )
        if not series:
            raise ValueError(
                f"strategy {settings.strategy_version} requires metrics for {args.symbol}"
            )
        metrics_by_symbol = {args.symbol: series}
    if args.exit_policy == "legacy-script":
        strategy = LegacyScriptExitSpikeBacktestStrategy(
            symbols=[args.symbol], total_notional=args.total_notional
        )
    elif args.research:
        from trading_platform.strategies.spike.research_premium import (
            ResearchParams,
            ResearchPremiumBacktestStrategy,
        )

        params = ResearchParams(
            triple_high_thresholds={
                "vwap_dev_5m": args.th_v,
                "ema_ratio_5m": args.th_e,
                "roc_5m": args.th_r,
            },
            atr_min=args.atr_min,
            premium_mult=args.premium_mult,
            premium_floor_pct=args.premium_floor,
            premium_cap_pct=args.premium_cap,
            stop_loss_pct=args.stop_pct,
            stop_check_after_ms=args.stop_check_ms,
            hold_ms=4 * 3600 * 1000,
        )
        if args.model_json:
            import json as _json

            model = _json.loads(Path(args.model_json).read_text(encoding="utf-8"))
            params.model_mean = model["mean"]
            params.model_std = model["std"]
            params.model_coefs = model["coefs"]
            params.model_intercept = float(model["intercept"])
        else:
            from trading_platform.research.up_premium_model import (
                COEFS,
                FEATURES as MODEL_FEATURES,
                INTERCEPT,
                MEAN,
                STD,
            )

            params.model_mean = {
                f: float(v) for f, v in zip(MODEL_FEATURES, MEAN)
            }
            params.model_std = {f: float(v) for f, v in zip(MODEL_FEATURES, STD)}
            params.model_coefs = {
                f: float(v) for f, v in zip(MODEL_FEATURES, COEFS)
            }
            params.model_intercept = float(INTERCEPT)
        strategy = ResearchPremiumBacktestStrategy(
            symbols=[args.symbol],
            total_notional=args.total_notional,
            research_params=params,
        )
    elif settings.strategy_definition.name == "pullback-v3":
        from trading_platform.strategies.spike.pullback import (
            PullbackV3BacktestStrategy,
        )

        initial_notional = (
            settings.capital_config.initial_trading_capital
            if settings.capital_config is not None
            else args.total_notional
        )
        v3_params = {
            "rise_3s_threshold": args.rise_3s_threshold,
            "vol_multiple": args.vol_multiple,
            "cooldown_seconds": args.cooldown_seconds,
            "min_spike_rise": args.min_spike_rise,
            "retrace_frac": args.retrace_frac,
            "buy_ratio_entry_min": args.buy_ratio_entry_min,
            "rise_low_lookback_hours": (
                settings.rise_low_lookback_minutes // 60
            ),
            "min_rise_duration_hours": (
                settings.min_rise_duration_minutes // 60
            ),
            "exit_strict_age_ms": settings.exit_strict_age_ms,
            "exit_flat_agreement": settings.exit_flat_agreement,
            "time_risk_grace_ms": settings.time_risk_grace_ms,
            "time_risk_grace_loss_ratio": settings.time_risk_grace_loss_ratio,
            "strong_strict_age_ms": settings.strong_strict_age_ms,
            "weak_strict_age_ms": settings.weak_strict_age_ms,
            "strong_bucket_strict_age_ms": settings.strong_bucket_strict_age_ms,
            "weak_bucket_strict_age_ms": settings.weak_bucket_strict_age_ms,
            "profit_unlock_ratio": settings.profit_unlock_ratio,
            "profit_drawdown_ratio": settings.profit_drawdown_ratio,
            "profit_drawdown_peak_ratio": settings.profit_drawdown_peak_ratio,
            "early_profit_unlock_ratio": settings.early_profit_unlock_ratio,
            "stop_5m_high": not args.no_stop_5m_high,
            "take_profit": args.take_profit,
            "max_hold_seconds": args.max_hold_seconds,
            "wait_seconds": args.wait_seconds,
        }
        strategy = PullbackV3BacktestStrategy(
            symbols=[args.symbol],
            total_notional=initial_notional,
            strategy_parameters={
                key: value for key, value in v3_params.items() if value is not None
            },
        )
    else:
        strategy_class = settings.strategy_definition.strategy_class
        if settings.prior_high_lookback_minutes == 0:
            strategy_class = no_prior_high_strategy_class(strategy_class)
        initial_notional = (
            settings.capital_config.initial_trading_capital
            if settings.capital_config is not None
            else args.total_notional
        )
        strategy = DynamicSpikeBacktestStrategy(
            symbols=[args.symbol],
            total_notional=initial_notional,
            exit_policy=(
                "candidate-v1"
                if args.exit_policy == "candidate-v1"
                else "execution-test-d007"
            ),
            prior_high_lookback_minutes=(
                settings.prior_high_lookback_minutes or 1
            ),
            entry_tier_mode=settings.entry_tier_mode,
            rise_low_lookback_minutes=settings.rise_low_lookback_minutes,
            min_rise_duration_minutes=settings.min_rise_duration_minutes,
            early_profit_unlock_ratio=settings.early_profit_unlock_ratio,
            strategy_class=strategy_class,
            strategy_parameters={
                key: value
                for key, value in {
                    "reject_below_current": settings.reject_below_current,
                    "entry_premium_mult": settings.entry_premium_mult,
                    "entry_premium_floor": settings.entry_premium_floor,
                    "entry_premium_cap": settings.entry_premium_cap,
                    "entry_premium_model": settings.entry_premium_model,
                    "entry_scoring_enabled": settings.entry_scoring_enabled,
                    "entry_scoring_threshold": settings.entry_scoring_threshold,
                    "entry_scoring_config": settings.entry_scoring_config,
                    "entry_premium_base_pct": settings.entry_premium_base_pct,
                    "oi_stop_enabled": settings.oi_stop_enabled,
                    "oi_stop_oi_rise_pct": settings.oi_stop_oi_rise_pct,
                    "oi_stop_loss_pct": settings.oi_stop_loss_pct,
                    "box_duration_min_minutes": settings.box_duration_min_minutes,
                    "spike_avg_deviation_max_pct": settings.spike_avg_deviation_max_pct,
                    "spike_range_max_pct": settings.spike_range_max_pct,
                    "spike_vwap_deviation_max_pct": settings.spike_vwap_deviation_max_pct,
                    "max_consecutive_up_minutes": settings.max_consecutive_up_minutes,
                    "group_rise_12h_threshold": settings.group_rise_12h_threshold,
                    "loose_consecutive_up_minutes": settings.loose_consecutive_up_minutes,
                    "loose_max_ls_ratio": settings.loose_max_ls_ratio,
                    "strong_tier_atr_shift": settings.strong_tier_atr_shift,
                    "exit_strict_age_ms": settings.exit_strict_age_ms,
                    "exit_flat_agreement": settings.exit_flat_agreement,
                    "time_risk_grace_ms": settings.time_risk_grace_ms,
                    "time_risk_grace_loss_ratio": settings.time_risk_grace_loss_ratio,
                    "strong_strict_age_ms": settings.strong_strict_age_ms,
                    "weak_strict_age_ms": settings.weak_strict_age_ms,
                    "strong_bucket_strict_age_ms": settings.strong_bucket_strict_age_ms,
                    "weak_bucket_strict_age_ms": settings.weak_bucket_strict_age_ms,
                    "profit_unlock_ratio": settings.profit_unlock_ratio,
                    "profit_drawdown_ratio": settings.profit_drawdown_ratio,
                    "profit_drawdown_peak_ratio": settings.profit_drawdown_peak_ratio,
                    "max_oi_change_pct": settings.max_oi_change_pct,
                    "max_ls_ratio": settings.max_ls_ratio,
                    "rise_5s_threshold": settings.rise_5s_threshold,
                    "accel_rise_5s_min": settings.accel_rise_5s_min,
                    "accel_ratio": settings.accel_ratio,
                    "accel_prev_minutes": settings.accel_prev_minutes,
                    "max_rise_5s_percent": settings.max_rise_5s_percent,
                    "max_rise_window_seconds": settings.max_rise_window_seconds,
                    "max_rise_window_percent": settings.max_rise_window_percent,
                    "max_volume_multiple_5s": settings.max_volume_multiple_5s,
                    "min_td_sell_setup_5m": settings.min_td_sell_setup_5m,
                    "min_volume_multiple_5m": settings.min_volume_multiple_5m,
                    "prior_high_tolerance_percent": settings.prior_high_tolerance_percent,
                    "metrics_series": (
                        metrics_by_symbol or {}
                    ).get(args.symbol),
                }.items()
                if key in (
                    settings.strategy_definition.supported_parameters
                    | settings.strategy_definition.internal_parameters
                )
            },
        )
        if settings.capital_config is not None:
            strategy = CapitalManagedSpikeStrategy(
                strategy, settings.capital_config
            )
    return BacktestEngine(
        events=events,
        strategy=strategy,
        config=config,
        symbol_rules=load_symbol_rules(args.exchange_info, [args.symbol]),
        execution_timeframe=settings.execution_timeframe,
    )


def save_backtest_result(result, output_path: Path) -> dict:
    analyzer = ResultAnalyzer(result)
    summary = analyzer.analyze()
    analyzer.save_results(
        str(output_path.parent), output_path.name, summary=summary
    )
    return summary


def main() -> None:
    args = parse_args()
    try:
        settings = resolve_settings(args)
    except ValueError as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(2) from error
    warmup_hours = (
        settings.start_ms - settings.load_start_ms
    ) / 3_600_000

    print("=== Dynamic Spike Short Strategy Backtest ===")
    print(f"Symbol: {args.symbol}")
    print(f"Period: {args.start} to {args.end}")
    print(f"Data source: {settings.duckdb_path}")
    prior_high_label = (
        "disabled" if settings.prior_high_lookback_minutes == 0
        else f"{settings.prior_high_lookback_minutes / 60:g}h"
    )
    print(f"Strategy version: {settings.strategy_version}")
    print(f"Prior high lookback: {prior_high_label}")
    print(f"Warmup: {warmup_hours:g}h")

    loader = BacktestDataLoader(
        duckdb_path=settings.duckdb_path,
        symbols=[args.symbol],
        start_ms=settings.load_start_ms,
        end_ms=settings.end_ms,
        require_aggtrades=settings.requires_bar1s,
        required_kline_intervals=list(settings.required_kline_intervals),
        archive_index_path=args.archive_index,
        bar1s_time_shift_ms=settings.bar1s_time_shift_ms,
        bar1s_feature_columns=getattr(
            settings.strategy_definition.data_requirements,
            "bar1s_feature_columns",
            None,
        ),
    )
    event_iter = loader.iter_all(
        chunk_hours=args.chunk_hours,
        fetch_batch_size=args.fetch_batch_size,
        duckdb_memory_limit=args.duckdb_memory_limit,
        duckdb_threads=args.duckdb_threads,
    )
    try:
        first_event = next(event_iter)
    except StopIteration:
        print("Error: no market data found in the requested range", file=sys.stderr)
        raise SystemExit(1)
    events = chain((first_event,), event_iter)

    result = create_spike_engine(
        args,
        settings,
        events=events,
    ).run()
    summary = save_backtest_result(result, settings.output_path)

    print("\n=== Backtest Results ===")
    print(f"Orders: {summary['orders']['total']}")
    print(f"Filled orders: {summary['orders']['filled']}")
    print(f"Positions: {summary['positions']['total']}")
    print(
        f"Full-position liquidation risk: "
        f"{summary['liquidation_risk']['total']} "
        f"({summary['liquidation_risk']['rate']:.2%})"
    )
    print(f"Net PnL: {summary['pnl']['net_pnl']:.2f} USDT")
    print(f"Results saved to: {settings.output_path}")


if __name__ == "__main__":
    main()
