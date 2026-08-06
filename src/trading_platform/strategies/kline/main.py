"""
K线策略群主程序
账户A - 定时器驱动的K线策略集合
"""
import asyncio
import logging
import os
import sys

from trading_platform.shared.config import BinanceConfig, RedisConfig, StrategyConfig
from trading_platform.shared.logging_config import setup_logger
from trading_platform.strategies.kline.example import ExampleKlineStrategy

logger = setup_logger('strategy_kline', logging.INFO)


async def main():
    """K线策略群主程序"""
    logger.info("=" * 60)
    logger.info("K线策略群启动 (账户A)")
    logger.info("=" * 60)

    # 加载配置
    binance_config = BinanceConfig()
    redis_config = RedisConfig()
    strategy_config = StrategyConfig()

    logger.info(f"账户ID: {strategy_config.account_id}")
    logger.info(f"行情层API: {strategy_config.market_api_url}")
    logger.info(f"Redis: {redis_config.host}:{redis_config.port}")

    # 初始化策略实例
    strategies = []

    # 示例策略1 - 5分钟K线突破
    strategy1 = ExampleKlineStrategy(
        strategy_name="kline_breakout_5m",
        consumer_id="kline_strategy_breakout_5m_001",
        symbols=["BTCUSDT", "ETHUSDT"],
        intervals=["5m"],
        account_id=strategy_config.account_id,
        binance_config=binance_config,
        redis_config=redis_config,
        strategy_config=strategy_config,
    )
    strategies.append(strategy1)

    # 示例策略2 - 15分钟K线突破
    strategy2 = ExampleKlineStrategy(
        strategy_name="kline_breakout_15m",
        consumer_id="kline_strategy_breakout_15m_001",
        symbols=["BTCUSDT"],
        intervals=["15m"],
        account_id=strategy_config.account_id,
        binance_config=binance_config,
        redis_config=redis_config,
        strategy_config=strategy_config,
    )
    strategies.append(strategy2)

    logger.info(f"已加载 {len(strategies)} 个策略")

    # 启动所有策略
    tasks = [strategy.start() for strategy in strategies]

    try:
        await asyncio.gather(*tasks)
    except KeyboardInterrupt:
        logger.info("收到停止信号，正在关闭策略...")
        for strategy in strategies:
            await strategy.stop()
        logger.info("所有策略已停止")
    except Exception as e:
        logger.error(f"策略运行异常: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
