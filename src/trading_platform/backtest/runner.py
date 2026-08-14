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
from itertools import chain
import json
import logging
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from trading_platform.shared.config import BacktestConfig
from trading_platform.shared.binance.symbol_rules import BinanceSymbolRuleBook
from trading_platform.shared.progress import TaskDashboard
from .loader import BacktestDataLoader
from .engine import BacktestEngine
from .result import ResultAnalyzer

_STRATEGY_FACTORIES = {}
_DEFAULTS_REGISTERED = False


def register_strategy(name: str, factory) -> None:
    """注册策略构造器；实验版本可在不修改核心入口的情况下接入。"""
    normalized = name.strip().lower()
    if not normalized:
        raise ValueError("strategy name must not be empty")
    _STRATEGY_FACTORIES[normalized] = factory


def _default_strategy_factories() -> None:
    global _DEFAULTS_REGISTERED
    if _DEFAULTS_REGISTERED:
        return
    from .example_strategies import DemoStrategy, MinimalStrategy
    register_strategy("demo", lambda account_id, **_: DemoStrategy(account_id=account_id))
    register_strategy("minimal", lambda account_id, **_: MinimalStrategy(account_id=account_id))
    register_strategy("spike", _build_spike_strategy)
    _DEFAULTS_REGISTERED = True


def _build_spike_strategy(account_id, *, symbols=None, total_notional=None, **_):
    if not symbols:
        raise ValueError("Spike strategy requires at least one symbol")
    if total_notional is None or total_notional <= 0:
        raise ValueError("Spike strategy requires a positive --total-notional")
    from trading_platform.strategies.spike.short import DynamicSpikeBacktestStrategy
    return DynamicSpikeBacktestStrategy(symbols=symbols, total_notional=total_notional)

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
      --duckdb-path data/market/candles/candles.duckdb \\
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
        '--duckdb-path',
        type=str,
        required=True,
        help='只读 DuckDB candles 归档路径',
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
        '--limit-fill-fraction',
        type=float,
        default=1.0,
        help='每根穿价 1s Bar 最多成交 LIMIT 原数量的比例（0, 1]，默认 1',
    )

    parser.add_argument(
        '--exchange-info',
        type=Path,
        default=None,
        help='可选 Binance exchangeInfo JSON 快照；提供后 replay 按真实 tick/step 量化',
    )
    parser.add_argument('--chunk-hours', type=float, default=24.0 * 180)
    parser.add_argument('--fetch-batch-size', type=int, default=10_000)
    parser.add_argument('--duckdb-memory-limit', default=None)
    parser.add_argument('--duckdb-threads', type=int, default=1)

    parser.add_argument(
        '--log-level',
        type=str,
        default='INFO',
        choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'],
        help='日志级别（默认: INFO）'
    )
    parser.add_argument(
        '--log-file',
        type=Path,
        default=None,
        help='完整 DEBUG 日志文件（默认: <output_dir>/backtest.log）'
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


def load_symbol_rules(
    path: Path | None, symbols: list[str]
) -> BinanceSymbolRuleBook | None:
    if path is None:
        return None
    payload = json.loads(path.read_text(encoding='utf-8'))
    return BinanceSymbolRuleBook.from_exchange_info(payload, symbols=symbols)


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
    _default_strategy_factories()
    factory = _STRATEGY_FACTORIES.get(strategy_name.strip().lower())
    if factory is None:
        raise ImportError(f"Unknown strategy: {strategy_name}")
    return factory(account_id, symbols=symbols, total_notional=total_notional)


def main():
    """
    主函数
    """
    args = parse_args()

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
    else:
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        output_dir = f"reports/backtest_{args.strategy}_{timestamp}"

    # 完整日志落文件，供 Agent 按需核验
    log_file = args.log_file or Path(output_dir) / "backtest.log"
    root_logger = logging.getLogger()
    console_level = getattr(logging, args.log_level)
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding='utf-8')
    except OSError as error:
        logger.error(f"日志文件创建失败: {error}")
        sys.exit(1)
    original_root_level = root_logger.level
    original_console_levels = {}
    root_logger.setLevel(logging.DEBUG)
    for handler in root_logger.handlers:
        if (
            isinstance(handler, logging.StreamHandler)
            and not isinstance(handler, logging.FileHandler)
        ):
            original_console_levels[handler] = handler.level
            handler.setLevel(console_level)
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(
        '%(asctime)s [%(levelname)s] %(name)s: %(message)s'
    ))
    root_logger.addHandler(file_handler)

    task_name = f"{args.strategy} {','.join(args.symbols)}"
    dashboard: TaskDashboard | None = None
    loading_strategy = False
    try:
        dashboard = TaskDashboard(
            title="backtest",
            total=None,
            stream=sys.stdout,
        )
        dashboard.start(detail=f"strategy={args.strategy} output={output_dir}")
        dashboard.task_start(task_name)

        logger.info("=" * 60)
        logger.info("回测引擎启动")
        logger.info("=" * 60)
        logger.info(f"策略: {args.strategy}")
        logger.info(f"币种: {args.symbols}")
        logger.info(f"时间范围: {args.start} ~ {args.end}")
        logger.info(f"数据源: {args.duckdb_path}")
        logger.info(f"输出目录: {output_dir}")
        logger.info(f"日志文件: {log_file}")
        logger.info("=" * 60)

        # 1. 加载数据
        logger.info("Step 1/4: 加载数据")
        loader = BacktestDataLoader(
            duckdb_path=args.duckdb_path,
            symbols=args.symbols,
            start_ms=load_start_ms,
            end_ms=end_ms,
            require_aggtrades=args.strategy == 'spike',
            required_kline_intervals=(
                ['1m', '5m'] if args.strategy == 'spike' else []
            ),
        )
        event_iter = loader.iter_all(
            chunk_hours=args.chunk_hours,
            fetch_batch_size=args.fetch_batch_size,
            duckdb_memory_limit=args.duckdb_memory_limit,
            duckdb_threads=args.duckdb_threads,
        )
        first_event = next(event_iter, None)
        if first_event is None:
            raise ValueError("no market data found in the requested range")
        events = chain((first_event,), event_iter)
        logger.info("数据加载完成：使用流式事件迭代器")

        # 2. 加载策略
        logger.info("Step 2/4: 加载策略")
        loading_strategy = True
        strategy = load_strategy(
            args.strategy,
            args.account_id,
            symbols=args.symbols,
            total_notional=args.total_notional,
        )
        loading_strategy = False

        # 3. 运行回测
        logger.info("Step 3/4: 运行回测")
        config = BacktestConfig(
            data_dir=args.duckdb_path,
            output_dir=output_dir,
            maker_fee_rate=args.maker_fee,
            taker_fee_rate=args.taker_fee,
            trading_start_ms=start_ms,
            limit_fill_fraction_per_bar=args.limit_fill_fraction,
        )
        engine = BacktestEngine(
            strategy=strategy,
            events=events,
            config=config,
            account_id=args.account_id,
            symbol_rules=load_symbol_rules(args.exchange_info, args.symbols),
        )
        result = engine.run()
        if result.virtual_time_end < start_ms:
            raise ValueError("只有预热数据，交易时间范围内没有事件")
        logger.info("回测运行完成")

        # 4. 分析结果
        logger.info("Step 4/4: 分析结果")
        analyzer = ResultAnalyzer(result)
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

        output_path = Path(output_dir)
        analyzer.save_results(str(output_path.parent), output_path.name)
        logger.info(f"结果已保存到: {output_dir}")
    except KeyboardInterrupt:
        if dashboard is not None:
            dashboard.task_failed(task_name)
            dashboard.close(status="interrupted")
        raise
    except Exception as error:
        if loading_strategy:
            logger.error(f"策略加载失败: {error}")
            logger.info(
                "\n提示：Spike 策略必须提供至少一个币种和正数 "
                "--total-notional。\n"
            )
        else:
            logger.error(f"回测失败: {error}", exc_info=True)
        if dashboard is not None:
            dashboard.task_failed(task_name)
            dashboard.close(status="failed")
        sys.exit(1)
    else:
        assert dashboard is not None
        dashboard.task_done(task_name, "OK")
        dashboard.close(status="ok", detail=f"output={output_dir}")
        logger.info("回测完成！")
    finally:
        try:
            root_logger.removeHandler(file_handler)
            file_handler.close()
        finally:
            root_logger.setLevel(original_root_level)
            for handler, level in original_console_levels.items():
                handler.setLevel(level)


if __name__ == '__main__':
    main()
