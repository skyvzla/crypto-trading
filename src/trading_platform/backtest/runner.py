"""
回测命令行入口

用法：
    python -m trading_platform.backtest.runner \\
        --config config/spike_v1.yaml \\
        --symbols BTCUSDT ETHUSDT \\
        --start 2026-06-01 \\
        --end 2026-07-01 \\
        --output reports/backtest_20260806
"""
import argparse
import logging
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from trading_platform.shared.config import BacktestConfig
from .loader import BacktestDataLoader
from .engine import BacktestEngine
from .result import ResultAnalyzer

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    """
    解析命令行参数

    Returns:
        参数命名空间
    """
    parser = argparse.ArgumentParser(
        description='回测引擎命令行工具',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 回测单币种
  python -m trading_platform.backtest.runner \\
      --strategy spike \\
      --symbols BTCUSDT \\
      --start 2026-06-01 \\
      --end 2026-06-02 \\
      --data-dir data/market \\
      --output reports/test_run

  # 回测多币种
  python -m trading_platform.backtest.runner \\
      --strategy spike \\
      --symbols BTCUSDT ETHUSDT SOLUSDT \\
      --start 2026-06-01 \\
      --end 2026-07-01 \\
      --output reports/multi_symbol_run
        """
    )

    parser.add_argument(
        '--strategy',
        type=str,
        required=True,
        help='策略名称（如 spike）'
    )

    parser.add_argument(
        '--symbols',
        type=str,
        nargs='+',
        required=True,
        help='币种列表（如 BTCUSDT ETHUSDT）'
    )

    parser.add_argument(
        '--start',
        type=str,
        required=True,
        help='开始日期（YYYY-MM-DD）'
    )

    parser.add_argument(
        '--end',
        type=str,
        required=True,
        help='结束日期（YYYY-MM-DD）'
    )

    parser.add_argument(
        '--data-dir',
        type=str,
        default='data/market',
        help='数据目录（默认: data/market）'
    )

    parser.add_argument(
        '--output',
        type=str,
        default=None,
        help='输出目录（默认: reports/backtest_<timestamp>）'
    )

    parser.add_argument(
        '--maker-fee',
        type=float,
        default=0.0002,
        help='Maker 费率（默认: 0.0002）'
    )

    parser.add_argument(
        '--taker-fee',
        type=float,
        default=0.0004,
        help='Taker 费率（默认: 0.0004）'
    )

    parser.add_argument(
        '--account-id',
        type=str,
        default='backtest',
        help='账户ID（默认: backtest）'
    )

    parser.add_argument(
        '--total-notional',
        type=Decimal,
        default=None,
        help='Spike 每轮信号总名义金额（使用 --strategy spike 时必填）'
    )

    parser.add_argument(
        '--warmup-hours',
        type=float,
        default=None,
        help='指标预热时长；Spike 默认 16 小时，其他策略默认 0'
    )

    parser.add_argument(
        '--log-level',
        type=str,
        default='INFO',
        choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'],
        help='日志级别（默认: INFO）'
    )

    return parser.parse_args()


def parse_date(date_str: str) -> int:
    """
    解析日期字符串为毫秒时间戳

    Args:
        date_str: 日期字符串（YYYY-MM-DD）

    Returns:
        毫秒时间戳
    """
    dt = datetime.strptime(date_str, '%Y-%m-%d').replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def load_strategy(
    strategy_name: str,
    account_id: str,
    symbols: list[str] | None = None,
    total_notional: Decimal | None = None,
):
    """
    动态加载策略实例

    Args:
        strategy_name: 策略名称
        account_id: 账户ID

    Returns:
        策略实例

    Raises:
        ImportError: 策略未找到
    """
    # 尝试导入策略
    try:
        if strategy_name == 'demo':
            # 导入演示策略
            from .example_strategies import DemoStrategy
            return DemoStrategy(account_id=account_id)

        elif strategy_name == 'minimal':
            # 导入最小化策略
            from .example_strategies import MinimalStrategy
            return MinimalStrategy(account_id=account_id)

        elif strategy_name == 'spike':
            if not symbols:
                raise ValueError("Spike strategy requires at least one symbol")
            if total_notional is None or total_notional <= 0:
                raise ValueError(
                    "Spike strategy requires a positive --total-notional"
                )

            from trading_platform.strategies.spike_short import (
                DynamicSpikeBacktestStrategy,
            )
            return DynamicSpikeBacktestStrategy(
                symbols=symbols,
                total_notional=total_notional,
            )
        else:
            raise ImportError(f"Unknown strategy: {strategy_name}")

    except ImportError as e:
        logger.error(f"Failed to load strategy '{strategy_name}': {e}")
        raise


def main():
    """
    主函数
    """
    args = parse_args()

    # 设置日志级别
    logging.getLogger().setLevel(args.log_level)

    # 解析时间范围
    start_ms = parse_date(args.start)
    end_ms = parse_date(args.end)
    warmup_hours = (
        args.warmup_hours
        if args.warmup_hours is not None
        else (16.0 if args.strategy == 'spike' else 0.0)
    )
    if warmup_hours < 0:
        logger.error("--warmup-hours 不能为负数")
        sys.exit(2)
    load_start_ms = start_ms - int(warmup_hours * 3_600_000)

    # 生成输出目录
    if args.output:
        output_dir = args.output
        run_id = Path(output_dir).name
    else:
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        run_id = f"backtest_{args.strategy}_{timestamp}"
        output_dir = f"reports/{run_id}"

    logger.info("=" * 60)
    logger.info("回测引擎启动")
    logger.info("=" * 60)
    logger.info(f"策略: {args.strategy}")
    logger.info(f"币种: {args.symbols}")
    logger.info(f"时间范围: {args.start} ~ {args.end}")
    logger.info(f"数据目录: {args.data_dir}")
    logger.info(f"输出目录: {output_dir}")
    logger.info("=" * 60)

    # 1. 加载数据
    logger.info("Step 1/4: 加载数据")
    loader = BacktestDataLoader(
        data_dir=args.data_dir,
        symbols=args.symbols,
        start_ms=load_start_ms,
        end_ms=end_ms,
        require_aggtrades=args.strategy == 'spike',
        required_kline_intervals=(
            ['1m', '5m'] if args.strategy == 'spike' else []
        ),
    )

    try:
        events = loader.load_all()
    except Exception as e:
        logger.error(f"数据加载失败: {e}")
        sys.exit(1)

    if not events:
        logger.error("未找到任何数据，请检查数据目录和时间范围")
        sys.exit(1)
    if not any(event.available_time >= start_ms for event in events):
        logger.error("只有预热数据，交易时间范围内没有事件")
        sys.exit(1)

    logger.info(f"数据加载完成: {len(events)} 个事件")

    # 2. 加载策略
    logger.info("Step 2/4: 加载策略")
    try:
        strategy = load_strategy(
            args.strategy,
            args.account_id,
            symbols=args.symbols,
            total_notional=args.total_notional,
        )
    except Exception as e:
        logger.error(f"策略加载失败: {e}")
        logger.info(
            "\n提示：Spike 策略必须提供至少一个币种和正数 "
            "--total-notional。\n"
        )
        sys.exit(1)

    # 3. 运行回测
    logger.info("Step 3/4: 运行回测")

    config = BacktestConfig(
        data_dir=args.data_dir,
        output_dir=output_dir,
        maker_fee_rate=args.maker_fee,
        taker_fee_rate=args.taker_fee,
        trading_start_ms=start_ms,
    )

    engine = BacktestEngine(
        strategy=strategy,
        events=events,
        config=config,
        account_id=args.account_id
    )

    try:
        result = engine.run()
    except Exception as e:
        logger.error(f"回测运行失败: {e}", exc_info=True)
        sys.exit(1)

    logger.info("回测运行完成")

    # 4. 分析结果
    logger.info("Step 4/4: 分析结果")

    analyzer = ResultAnalyzer(result)

    # 打印摘要
    summary = analyzer.analyze()

    logger.info("=" * 60)
    logger.info("回测结果摘要")
    logger.info("=" * 60)
    logger.info(f"订单总数: {summary['orders']['total']}")
    logger.info(f"  - 成交: {summary['orders']['filled']}")
    logger.info(f"  - 撤销: {summary['orders']['cancelled']}")
    logger.info(f"  - 过期: {summary['orders']['expired']}")
    logger.info(f"  - 成交率: {summary['orders']['fill_rate']:.2%}")
    logger.info("")
    logger.info(f"持仓总数: {summary['positions']['total']}")
    logger.info(f"  - 未平仓: {summary['positions']['open']}")
    logger.info(f"  - 已平仓: {summary['positions']['closed']}")
    logger.info(f"  - 盈利: {summary['positions']['profitable']}")
    logger.info(f"  - 亏损: {summary['positions']['loss']}")
    logger.info(f"  - 胜率: {summary['positions']['win_rate']:.2%}")
    logger.info("")
    logger.info(f"总盈亏: {summary['pnl']['net_pnl']:.2f} USDT")
    logger.info(f"  - 未实现盈亏: {summary['pnl']['total_unrealized']:.2f} USDT")
    logger.info(f"  - 盈利总额: {summary['pnl']['total_profit']:.2f} USDT")
    logger.info(f"  - 亏损总额: {summary['pnl']['total_loss']:.2f} USDT")
    logger.info(f"  - 手续费: {summary['pnl']['total_commission']:.2f} USDT")
    logger.info(f"Profit Factor: {summary['pnl']['profit_factor']:.2f}")
    logger.info(f"最大回撤: {summary['pnl']['max_drawdown']:.2f} USDT")
    logger.info(f"Sharpe Ratio: {summary['pnl']['sharpe_ratio']:.2f}")
    logger.info("=" * 60)

    # 保存结果
    try:
        output_path = Path(output_dir)
        analyzer.save_results(str(output_path.parent), output_path.name)
        logger.info(f"结果已保存到: {output_dir}")
    except Exception as e:
        logger.error(f"保存结果失败: {e}", exc_info=True)
        sys.exit(1)

    logger.info("回测完成！")


if __name__ == '__main__':
    main()
