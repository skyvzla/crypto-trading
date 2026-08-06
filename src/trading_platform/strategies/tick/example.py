"""
1s 事件策略示例
演示如何继承 TickStrategyBase 实现具体策略
"""
import logging
from decimal import Decimal

from trading_platform.shared.events import Bar1s, OrderIntent
from trading_platform.strategies.tick import TickStrategyBase

logger = logging.getLogger(__name__)


class ExampleTickStrategy(TickStrategyBase):
    """
    简单的 1s 事件策略示例

    逻辑：
    - 监控 1s Bar 的价格变化
    - 当价格突破某个阈值时打印日志
    - 演示如何发送订单意图
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # 策略参数
        self.price_threshold = Decimal('0.001')  # 1秒涨跌幅阈值（0.1%）

        # 状态追踪
        self.last_prices: dict[str, Decimal] = {}

    async def on_bar1s(self, bar: Bar1s) -> None:
        """
        处理 1s Bar 事件

        Args:
            bar: 1s Bar 数据
        """
        symbol = bar.symbol
        close_price = bar.close

        logger.info(
            f"[{symbol}] Bar1s: "
            f"timestamp={bar.timestamp} "
            f"O={bar.open} H={bar.high} L={bar.low} C={bar.close} "
            f"V={bar.volume} trades={bar.trade_count}"
        )

        # 检查是否有上一次的价格
        if symbol in self.last_prices:
            last_price = self.last_prices[symbol]
            price_change = (close_price - last_price) / last_price

            # 突破阈值
            if abs(price_change) >= self.price_threshold:
                direction = "UP" if price_change > 0 else "DOWN"
                logger.warning(
                    f"[{symbol}] Price {direction}: "
                    f"{last_price} -> {close_price} "
                    f"({price_change*100:.2f}%)"
                )

                # 这里可以生成订单意图
                # order_intent = OrderIntent(
                #     account_id=self.account_id,
                #     strategy=self.strategy_name,
                #     strategy_abbrev="exm",
                #     event_id=f"{symbol}_{bar.timestamp}",
                #     event_ts_s=bar.timestamp // 1000,
                #     tier=1,
                #     symbol=symbol,
                #     side='BUY' if price_change > 0 else 'SELL',
                #     price=close_price * Decimal('0.99'),  # 挂单在略低的价格
                #     quantity=Decimal('0.001'),
                #     reduce_only=False,
                # )
                # logger.info(f"Generated order intent: {order_intent}")

        # 更新最后价格
        self.last_prices[symbol] = close_price


async def main():
    """运行示例策略"""
    from trading_platform.shared.config import BinanceConfig, RedisConfig, StrategyConfig

    strategy = ExampleTickStrategy(
        strategy_name="example_tick",
        consumer_id="tick_strategy_example_001",
        symbols=["BTCUSDT", "ETHUSDT"],
        account_id="test_account",
        binance_config=BinanceConfig(),
        redis_config=RedisConfig(),
        strategy_config=StrategyConfig(account_id="test_account"),
    )

    try:
        await strategy.start()
        logger.info("Strategy started, press Ctrl+C to stop")
        await asyncio.Event().wait()  # 等待手动中断
    except KeyboardInterrupt:
        logger.info("Stopping strategy...")
    finally:
        await strategy.stop()


if __name__ == "__main__":
    import asyncio
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
    )
    asyncio.run(main())
