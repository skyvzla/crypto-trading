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
    entry_tier_mode: str
    early_profit_unlock_ratio: Decimal | None
    max_consecutive_up_minutes: int
    max_oi_change_pct: float
    max_ls_ratio: float
    rise_5s_threshold: Decimal
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

    返回按快照时间升序的 [(snapshot_ms, oi, ls_ratio)]；归档缺失或为空时返回空列表。
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
        required=True,
        help="Total notional allocated to each signal",
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
    parser.add_argument(
        "--entry-tier-mode",
        choices=("three-tier", "tier3-only"),
        default=None,
        help="入场挂单模式；默认由策略声明决定",
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
        help="DuckDB 流式回测的时间窗口（小时，默认 180 天）",
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
    if args.total_notional <= 0:
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
    rise_5s_threshold = (
        Decimal(str(args.rise_5s_threshold_percent)) / Decimal("100")
        if args.rise_5s_threshold_percent is not None else Decimal("0.05")
    )
    prior_high_tolerance_percent = (
        args.prior_high_tolerance_percent
        if args.prior_high_tolerance_percent is not None else Decimal("0")
    )
    profit_unlock_percent = args.profit_unlock_percent
    if profit_unlock_percent is None and defaults.profit_unlock_percent is not None:
        profit_unlock_percent = Decimal(str(defaults.profit_unlock_percent))

    optional_parameters = {
        "max_consecutive_up_minutes": args.max_consecutive_up_minutes,
        "max_oi_change_pct": args.max_oi_change_pct,
        "max_ls_ratio": args.max_ls_ratio,
        "rise_5s_threshold_percent": args.rise_5s_threshold_percent,
        "prior_high_tolerance_percent": args.prior_high_tolerance_percent,
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

    if prior_high_lookback_hours < 0:
        raise ValueError("--prior-high-lookback-hours must not be negative")
    if rise_low_lookback_hours < 0 or min_rise_duration_hours < 0:
        raise ValueError("rise lookback and minimum duration must not be negative")
    if (rise_low_lookback_hours == 0) != (min_rise_duration_hours == 0):
        raise ValueError("rise lookback and minimum duration must both be zero or positive")
    if min_rise_duration_hours > rise_low_lookback_hours:
        raise ValueError("minimum rise duration must not exceed rise lookback")
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
        entry_tier_mode=entry_tier_mode,
        early_profit_unlock_ratio=(
            profit_unlock_percent / Decimal("100")
            if profit_unlock_percent is not None
            else None
        ),
        max_consecutive_up_minutes=args.max_consecutive_up_minutes,
        max_oi_change_pct=args.max_oi_change_pct,
        max_ls_ratio=args.max_ls_ratio,
        rise_5s_threshold=rise_5s_threshold,
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
    metrics_series = None
    if settings.strategy_definition.data_requirements.metrics_5m:
        series = load_metrics_series(args.metrics_root, args.symbol)
        if not series:
            raise ValueError(
                f"strategy {settings.strategy_version} requires metrics for {args.symbol}"
            )
        metrics_series = {args.symbol: series}
    if args.exit_policy == "legacy-script":
        strategy = LegacyScriptExitSpikeBacktestStrategy(
            symbols=[args.symbol], total_notional=args.total_notional
        )
    else:
        strategy_class = settings.strategy_definition.strategy_class
        if settings.prior_high_lookback_minutes == 0:
            strategy_class = no_prior_high_strategy_class(strategy_class)
        strategy = DynamicSpikeBacktestStrategy(
            symbols=[args.symbol],
            total_notional=args.total_notional,
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
                    "max_consecutive_up_minutes": settings.max_consecutive_up_minutes,
                    "max_oi_change_pct": settings.max_oi_change_pct,
                    "max_ls_ratio": settings.max_ls_ratio,
                    "rise_5s_threshold": settings.rise_5s_threshold,
                    "prior_high_tolerance_percent": settings.prior_high_tolerance_percent,
                    "metrics_series": (
                        metrics_series or {}
                    ).get(args.symbol),
                }.items()
                if key in (
                    settings.strategy_definition.supported_parameters
                    | settings.strategy_definition.internal_parameters
                )
            },
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
    analyzer.save_results(str(output_path.parent), output_path.name)
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
