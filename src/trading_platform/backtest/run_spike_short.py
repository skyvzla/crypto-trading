#!/usr/bin/env python3
"""Dynamic Spike Short 策略专用回测入口。"""
import argparse
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from trading_platform.backtest.engine import BacktestEngine
from trading_platform.backtest.loader import BacktestDataLoader
from trading_platform.backtest.result import ResultAnalyzer
from trading_platform.backtest.runner import load_symbol_rules
from trading_platform.shared.config import BacktestConfig
from trading_platform.strategies.spike_legacy_research import (
    LegacyScriptExitSpikeBacktestStrategy,
)
from trading_platform.strategies.spike_short import DynamicSpikeBacktestStrategy


def _timestamp_ms(value: str) -> int:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def parse_args() -> argparse.Namespace:
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
    source_group = parser.add_mutually_exclusive_group()
    source_group.add_argument(
        "--data-dir", default=None, help="Parquet market data directory"
    )
    source_group.add_argument(
        "--duckdb-path",
        default=None,
        help="Read-only DuckDB candles archive",
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
        default="confirmed",
        help="Exit policy; legacy-script is replay research only",
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
        "--prior-high-lookback-hours",
        type=int,
        default=4,
        help="前高过滤回看周期（小时），默认 4",
    )
    parser.add_argument(
        "--exchange-info",
        type=Path,
        default=None,
        help="可选 Binance exchangeInfo JSON 快照，用于 tick/step 量化",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.total_notional <= 0:
        print("Error: --total-notional must be positive", file=sys.stderr)
        raise SystemExit(2)

    start_ms = _timestamp_ms(args.start)
    end_ms = _timestamp_ms(args.end)
    if start_ms >= end_ms:
        print("Error: --start must be earlier than --end", file=sys.stderr)
        raise SystemExit(2)
    if args.warmup_hours < 0:
        print("Error: --warmup-hours must not be negative", file=sys.stderr)
        raise SystemExit(2)
    if args.prior_high_lookback_hours <= 0:
        print("Error: --prior-high-lookback-hours must be positive", file=sys.stderr)
        raise SystemExit(2)
    warmup_hours = max(args.warmup_hours, float(args.prior_high_lookback_hours))
    load_start_ms = start_ms - int(warmup_hours * 3_600_000)
    bar1s_time_shift_ms = int(args.bar1s_time_shift_hours * Decimal("3600000"))
    prior_high_lookback_minutes = args.prior_high_lookback_hours * 60
    data_dir = args.data_dir or "data/market"
    data_source = args.duckdb_path or data_dir

    print("=== Dynamic Spike Short Strategy Backtest ===")
    print(f"Symbol: {args.symbol}")
    print(f"Period: {args.start} to {args.end}")
    print(f"Data source: {data_source}")
    print(f"Prior high lookback: {args.prior_high_lookback_hours}h")
    print(f"Warmup: {warmup_hours:g}h")

    loader = BacktestDataLoader(
        data_dir=data_dir,
        symbols=[args.symbol],
        start_ms=load_start_ms,
        end_ms=end_ms,
        require_aggtrades=True,
        required_kline_intervals=(
            ["1m", "5m", "15m"]
            if args.exit_policy == "candidate-v1"
            else ["1m", "5m"]
        ),
        duckdb_path=args.duckdb_path,
        bar1s_time_shift_ms=bar1s_time_shift_ms,
    )
    events = loader.load_all()
    if not events:
        print("Error: no market data found in the requested range", file=sys.stderr)
        raise SystemExit(1)
    if not any(event.available_time >= start_ms for event in events):
        print("Error: no events in the trading period", file=sys.stderr)
        raise SystemExit(1)

    output_path = Path(args.output)
    config = BacktestConfig(
        data_dir=data_source,
        output_dir=str(output_path),
        trading_start_ms=start_ms,
        limit_fill_fraction_per_bar=args.limit_fill_fraction,
        bar1s_time_shift_ms=bar1s_time_shift_ms,
        prior_high_lookback_minutes=prior_high_lookback_minutes,
    )
    if args.exit_policy == "legacy-script":
        strategy = LegacyScriptExitSpikeBacktestStrategy(
            symbols=[args.symbol], total_notional=args.total_notional
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
            prior_high_lookback_minutes=prior_high_lookback_minutes,
        )
    result = BacktestEngine(
        events=events,
        strategy=strategy,
        config=config,
        symbol_rules=load_symbol_rules(args.exchange_info, [args.symbol]),
    ).run()

    analyzer = ResultAnalyzer(result)
    summary = analyzer.analyze()
    analyzer.save_results(str(output_path.parent), output_path.name)

    print("\n=== Backtest Results ===")
    print(f"Orders: {summary['orders']['total']}")
    print(f"Filled orders: {summary['orders']['filled']}")
    print(f"Positions: {summary['positions']['total']}")
    print(f"Net PnL: {summary['pnl']['net_pnl']:.2f} USDT")
    print(f"Results saved to: {output_path}")


if __name__ == "__main__":
    main()
