"""
1s事件策略群主程序
账户B - Redis Pub/Sub驱动的实时策略集合
"""
import asyncio
import logging
import os
import sys

from trading_platform.shared.config import BinanceConfig, RedisConfig, StrategyConfig
from trading_platform.shared.logging_config import setup_logger
from trading_platform.strategies.tick.example import ExampleTickStrategy

logger = setup_logger('strategy_tick', logging.INFO)


async def main():
    """1s事件策略群主程序"""
    logger.info("=" * 60)
    logger.info("1s事件策略群启动 (账户B)")
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

    # 示例策略1 - 1s动量策略
    strategy1 = ExampleTickStrategy(
        strategy_name="tick_momentum",
        consumer_id="tick_strategy_momentum_001",
        symbols=["BTCUSDT", "ETHUSDT"],
        account_id=strategy_config.account_id,
        binance_config=binance_config,
        redis_config=redis_config,
        strategy_config=strategy_config,
    )
    strategies.append(strategy1)

    # 示例策略2 - 1s套利策略
    strategy2 = ExampleTickStrategy(
        strategy_name="tick_arbitrage",
        consumer_id="tick_strategy_arbitrage_001",
        symbols=["BTCUSDT"],
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
