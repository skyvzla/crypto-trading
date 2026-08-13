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


class NoPriorHighDynamicSpikeShortStrategy(DynamicSpikeShortStrategy):
    """回测实验用适配器：保留信号逻辑，但不施加前高价格约束。"""

    def __init__(self, *args, **kwargs):
        # 父类要求 lookback 为正；实际比较在 _prior_high_point 中被禁用。
        kwargs["prior_high_lookback_minutes"] = 1
        super().__init__(*args, **kwargs)
        self.prior_high_lookback_minutes = 0

    def _prior_high_point(self, minute_start: int):
        # 返回正价格下永远不会拦截入场的哨兵值，避免改动生产策略。
        return Decimal("0"), minute_start

    def _detect_signal(self, bar):
        signal = super()._detect_signal(bar)
        if signal is not None:
            signal.prior_high = None
            signal.prior_high_time = None
        return signal


class NoPriorHighDynamicSpikeBacktestStrategy(DynamicSpikeBacktestStrategy):
    """多币种适配器的无前高过滤实验版本。"""

    def __init__(
        self,
        symbols,
        total_notional,
        account=None,
        exit_policy="execution-test-d007",
        entry_tier_mode="three-tier",
        rise_low_lookback_minutes=0,
        min_rise_duration_minutes=0,
        early_profit_unlock_ratio=None,
        max_consecutive_up_minutes=0,
        max_oi_change_pct=0.0,
        max_ls_ratio=0.0,
        metrics_series=None,
        strategy_version="v1",
    ):
        self.strategies = {
            symbol: NoPriorHighDynamicSpikeShortStrategy(
                symbol,
                total_notional=total_notional,
                account=account,
                exit_policy=exit_policy,
                entry_tier_mode=entry_tier_mode,
                rise_low_lookback_minutes=rise_low_lookback_minutes,
                min_rise_duration_minutes=min_rise_duration_minutes,
                early_profit_unlock_ratio=early_profit_unlock_ratio,
                max_consecutive_up_minutes=max_consecutive_up_minutes,
                max_oi_change_pct=max_oi_change_pct,
                max_ls_ratio=max_ls_ratio,
                metrics_series=(metrics_series or {}).get(symbol),
                strategy_version=strategy_version,
            )
            for symbol in symbols
        }
        self._account = account
        self._entry_enabled = True
        self._blocked_entry_symbols = frozenset()
        self.active_symbol = None


@dataclass(frozen=True)
class SpikeBacktestSettings:
    strategy_version: str
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
    required_kline_intervals: tuple[str, ...]
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
        help="Exit policy; v2默认candidate-v1，v1默认confirmed",
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
        "--strategy-version",
        choices=("v1", "v2"),
        default="v1",
        help="策略版本；v2 默认使用6h前高、7天低点上涨24h、第三档全仓和1.5%%盈利解锁",
    )
    parser.add_argument(
        "--prior-high-lookback-hours",
        type=int,
        default=None,
        help="前高过滤回看周期（小时），0 表示禁用；默认由策略版本决定",
    )
    parser.add_argument(
        "--rise-low-lookback-hours",
        type=int,
        default=None,
        help="上涨起点最低价的回看窗口（小时）；v2默认168，v1默认禁用",
    )
    parser.add_argument(
        "--min-rise-duration-hours",
        type=int,
        default=None,
        help="窗口最低点距信号的最短小时数；v2默认24，v1默认禁用",
    )
    parser.add_argument(
        "--entry-tier-mode",
        choices=("three-tier", "tier3-only"),
        default=None,
        help="入场挂单模式；v2默认只挂第三档全仓，v1默认三档",
    )
    parser.add_argument(
        "--profit-unlock-percent",
        type=Decimal,
        default=None,
        help="持仓价格盈利超过该百分比后永久解除前90秒风险保护；v2默认1.5",
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
        "--exchange-info",
        type=Path,
        default=None,
        help="可选 Binance exchangeInfo JSON 快照，用于 tick/step 量化",
    )
    parser.add_argument(
        "--chunk-hours",
        type=float,
        default=24.0 * 90,
        help="DuckDB 流式回测的时间窗口（小时）",
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
        help="单个 DuckDB worker 的内存上限，例如 4GB",
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
    is_v2 = args.strategy_version == "v2"
    if args.exit_policy is None:
        args.exit_policy = "candidate-v1" if is_v2 else "confirmed"
    prior_high_lookback_hours = args.prior_high_lookback_hours
    if prior_high_lookback_hours is None:
        prior_high_lookback_hours = 6 if is_v2 else 4
    rise_low_lookback_hours = args.rise_low_lookback_hours
    if rise_low_lookback_hours is None:
        rise_low_lookback_hours = 7 * 24 if is_v2 else 0
    min_rise_duration_hours = args.min_rise_duration_hours
    if min_rise_duration_hours is None:
        min_rise_duration_hours = 24 if is_v2 else 0
    entry_tier_mode = args.entry_tier_mode or (
        "tier3-only" if is_v2 else "three-tier"
    )
    profit_unlock_percent = args.profit_unlock_percent
    if profit_unlock_percent is None and is_v2:
        profit_unlock_percent = Decimal("1.5")

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
        strategy_version=args.strategy_version,
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
        required_kline_intervals=(
            ("1m", "5m", "15m")
            if args.exit_policy == "candidate-v1"
            else ("1m", "5m")
        ),
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
    if args.metrics_root is not None and (
        settings.max_oi_change_pct > 0 or settings.max_ls_ratio > 0
    ):
        series = load_metrics_series(args.metrics_root, args.symbol)
        if series:
            metrics_series = {args.symbol: series}
    if args.exit_policy == "legacy-script":
        strategy = LegacyScriptExitSpikeBacktestStrategy(
            symbols=[args.symbol], total_notional=args.total_notional
        )
    elif settings.prior_high_lookback_minutes == 0:
        strategy = NoPriorHighDynamicSpikeBacktestStrategy(
            symbols=[args.symbol],
            total_notional=args.total_notional,
            exit_policy=(
                "candidate-v1"
                if args.exit_policy == "candidate-v1"
                else "execution-test-d007"
            ),
            entry_tier_mode=settings.entry_tier_mode,
            rise_low_lookback_minutes=settings.rise_low_lookback_minutes,
            min_rise_duration_minutes=settings.min_rise_duration_minutes,
            early_profit_unlock_ratio=settings.early_profit_unlock_ratio,
            max_consecutive_up_minutes=settings.max_consecutive_up_minutes,
            max_oi_change_pct=settings.max_oi_change_pct,
            max_ls_ratio=settings.max_ls_ratio,
            metrics_series=metrics_series,
            strategy_version=settings.strategy_version,
        )
    else:
        strategy = DynamicSpikeBacktestStrategy(
            symbols=[args.symbol],
            total_notional=args.total_notional,
            exit_policy=(
                "candidate-v1"
                if args.exit_policy == "candidate-v1"
                else "execution-test-d007"
            ),
            prior_high_lookback_minutes=settings.prior_high_lookback_minutes,
            entry_tier_mode=settings.entry_tier_mode,
            rise_low_lookback_minutes=settings.rise_low_lookback_minutes,
            min_rise_duration_minutes=settings.min_rise_duration_minutes,
            early_profit_unlock_ratio=settings.early_profit_unlock_ratio,
            max_consecutive_up_minutes=settings.max_consecutive_up_minutes,
            max_oi_change_pct=settings.max_oi_change_pct,
            max_ls_ratio=settings.max_ls_ratio,
            metrics_series=metrics_series,
            strategy_version=settings.strategy_version,
        )
    return BacktestEngine(
        events=events,
        strategy=strategy,
        config=config,
        symbol_rules=load_symbol_rules(args.exchange_info, [args.symbol]),
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
        require_aggtrades=True,
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
    print(f"Net PnL: {summary['pnl']['net_pnl']:.2f} USDT")
    print(f"Results saved to: {settings.output_path}")


if __name__ == "__main__":
    main()
