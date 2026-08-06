"""
K线策略示例
演示如何继承 KlineStrategyBase 实现具体策略
"""
import logging
from decimal import Decimal
from collections import deque

from trading_platform.shared.events import Kline, OrderIntent
from trading_platform.strategies.kline import KlineStrategyBase

logger = logging.getLogger(__name__)


class ExampleKlineStrategy(KlineStrategyBase):
    """
    简单的 K 线策略示例

    逻辑：
    - 监控 5 分钟 K 线
    - 计算简单移动平均线（SMA）
    - 当价格突破均线时打印日志
    - 演示如何发送订单意图
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # 策略参数
        self.sma_period = 10  # 移动平均周期

        # 状态追踪：每个交易对维护一个价格队列
        self.price_history: dict[str, deque] = {}

    async def on_timer(self, interval: str) -> None:
        """
        定时器触发回调

        Args:
            interval: 触发的 K 线周期
        """
        logger.debug(f"Timer triggered for {interval}")

    async def on_kline(self, kline: Kline) -> None:
        """
        处理 K 线事件

        Args:
            kline: K 线数据
        """
        symbol = kline.symbol
        interval = kline.interval
        close_price = kline.close

        logger.info(
            f"[{symbol}] Kline {interval}: "
            f"open_time={kline.open_time} close_time={kline.close_time} "
            f"O={kline.open} H={kline.high} L={kline.low} C={kline.close} "
            f"V={kline.volume}"
        )

        # 初始化价格历史
        key = f"{symbol}_{interval}"
        if key not in self.price_history:
            self.price_history[key] = deque(maxlen=self.sma_period)

        history = self.price_history[key]
        history.append(close_price)

        # 计算 SMA（需要足够的历史数据）
        if len(history) == self.sma_period:
            sma = sum(history) / self.sma_period

            logger.info(f"[{symbol}] SMA({self.sma_period}): {sma:.2f}, Current: {close_price:.2f}")

            # 检查突破
            if close_price > sma:
                diff_pct = (close_price - sma) / sma * 100
                if diff_pct > Decimal('0.5'):  # 突破 0.5%
                    logger.warning(
                        f"[{symbol}] Price ABOVE SMA: "
                        f"price={close_price} sma={sma:.2f} "
                        f"(+{diff_pct:.2f}%)"
                    )

                    # 这里可以生成订单意图
                    # order_intent = OrderIntent(
                    #     account_id=self.account_id,
                    #     strategy=self.strategy_name,
                    #     strategy_abbrev="exk",
                    #     event_id=f"{symbol}_{kline.close_time}",
                    #     event_ts_s=kline.close_time // 1000,
                    #     tier=1,
                    #     symbol=symbol,
                    #     side='BUY',
                    #     price=close_price * Decimal('0.999'),
                    #     quantity=Decimal('0.001'),
                    #     reduce_only=False,
                    # )
                    # logger.info(f"Generated order intent: {order_intent}")

            elif close_price < sma:
                diff_pct = (sma - close_price) / sma * 100
                if diff_pct > Decimal('0.5'):
                    logger.warning(
                        f"[{symbol}] Price BELOW SMA: "
                        f"price={close_price} sma={sma:.2f} "
                        f"(-{diff_pct:.2f}%)"
                    )


async def main():
    """运行示例策略"""
    from trading_platform.shared.config import BinanceConfig, RedisConfig, StrategyConfig

    strategy = ExampleKlineStrategy(
        strategy_name="example_kline",
        consumer_id="kline_strategy_example_001",
        symbols=["BTCUSDT", "ETHUSDT"],
        intervals=["5m", "15m"],  # 订阅 5 分钟和 15 分钟 K 线
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
