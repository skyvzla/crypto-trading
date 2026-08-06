#!/usr/bin/env python3
"""
Dynamic Spike Short Strategy - 回测运行脚本

使用新平台的回测引擎运行逼空做空策略
"""
import argparse
import sys
from pathlib import Path
from datetime import datetime, timezone

from trading_platform.backtest.loader import BacktestDataLoader
from trading_platform.backtest.engine import BacktestEngine
from trading_platform.backtest.result import BacktestResult
from trading_platform.strategies.spike_short import DynamicSpikeBacktestStrategy


def main():
    parser = argparse.ArgumentParser(description="Dynamic Spike Short Strategy Backtest")
    parser.add_argument("--symbol", default="AKEUSDT", help="Trading symbol")
    parser.add_argument("--start", required=True, help="Start date (ISO format: 2026-07-06T00:00:00+00:00)")
    parser.add_argument("--end", required=True, help="End date (ISO format: 2026-08-03T00:00:00+00:00)")
    parser.add_argument("--data-dir", default="data/market", help="Market data directory")
    parser.add_argument("--output", default="reports/spike_short_backtest.json", help="Output file")

    args = parser.parse_args()

    # 解析时间
    start_time = datetime.fromisoformat(args.start)
    end_time = datetime.fromisoformat(args.end)

    print(f"=== Dynamic Spike Short Strategy Backtest ===")
    print(f"Symbol: {args.symbol}")
    print(f"Period: {start_time} to {end_time}")
    print(f"Data directory: {args.data_dir}")
    print()

    # 1. 加载数据
    print("Loading market data...")
    loader = BacktestDataLoader(data_dir=Path(args.data_dir))

    # 需要加载：
    # - 1秒aggTrade (主要信号检测)
    # - 1分钟K线 (12小时低点、origin价格)
    # - 5分钟K线 (ATR计算)

    try:
        events = loader.load_backtest_data(
            symbols=[args.symbol],
            start_time=int(start_time.timestamp() * 1000),
            end_time=int(end_time.timestamp() * 1000),
            load_bars=True,  # 加载1s Bar
            load_klines={"1m", "5m"},  # 加载1m和5m K线
        )
        print(f"Loaded {len(events)} events")
    except Exception as e:
        print(f"Error loading data: {e}")
        sys.exit(1)

    # 2. 初始化策略
    print("\nInitializing strategy...")
    strategy = DynamicSpikeBacktestStrategy(symbols=[args.symbol])

    # 3. 运行回测
    print("Running backtest...")
    engine = BacktestEngine(
        events=events,
        strategy=strategy,
        initial_balance=10000.0,  # 初始权益10000 USDT
        maker_fee_rate=0.0002,
        taker_fee_rate=0.0004,
    )

    result = engine.run()

    # 4. 生成报告
    print("\n=== Backtest Results ===")
    print(f"Total trades: {len(result.trades)}")
    print(f"Winning trades: {result.stats.get('winning_trades', 0)}")
    print(f"Losing trades: {result.stats.get('losing_trades', 0)}")
    print(f"Win rate: {result.stats.get('win_rate', 0):.2%}")
    print(f"Total PnL: {result.stats.get('total_pnl', 0):.2f} USDT")
    print(f"Profit factor: {result.stats.get('profit_factor', 0):.2f}")
    print(f"Max drawdown: {result.stats.get('max_drawdown', 0):.2%}")
    print(f"Sharpe ratio: {result.stats.get('sharpe_ratio', 0):.2f}")

    # 5. 保存结果
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    result.save_json(output_path)
    print(f"\nResults saved to: {output_path}")

    # 6. 生成CSV报告（兼容原有格式）
    csv_path = output_path.with_suffix('.csv')
    result.save_csv(csv_path)
    print(f"CSV report saved to: {csv_path}")


if __name__ == "__main__":
    main()
