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
from trading_platform.shared.config import BacktestConfig
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
    parser.add_argument(
        "--data-dir", default="data/market", help="Market data directory"
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
    load_start_ms = start_ms - int(args.warmup_hours * 3_600_000)

    print("=== Dynamic Spike Short Strategy Backtest ===")
    print(f"Symbol: {args.symbol}")
    print(f"Period: {args.start} to {args.end}")
    print(f"Data directory: {args.data_dir}")

    loader = BacktestDataLoader(
        data_dir=args.data_dir,
        symbols=[args.symbol],
        start_ms=load_start_ms,
        end_ms=end_ms,
        require_aggtrades=True,
        required_kline_intervals=["1m", "5m"],
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
        data_dir=args.data_dir,
        output_dir=str(output_path),
        trading_start_ms=start_ms,
    )
    strategy = DynamicSpikeBacktestStrategy(
        symbols=[args.symbol],
        total_notional=args.total_notional,
    )
    result = BacktestEngine(
        events=events,
        strategy=strategy,
        config=config,
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
