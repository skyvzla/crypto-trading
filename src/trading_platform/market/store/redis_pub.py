"""
Redis Pub/Sub 发布器
发布 1s Bar 到 bar1s:{symbol} 通道
"""
import json
import logging
from decimal import Decimal

import redis.asyncio as redis

from trading_platform.shared.events import Bar1s


logger = logging.getLogger(__name__)


class DecimalEncoder(json.JSONEncoder):
    """处理 Decimal 类型的 JSON 编码器"""

    def default(self, obj):
        if isinstance(obj, Decimal):
            return str(obj)
        return super().default(obj)


class RedisPublisher:
    """
    Redis Pub/Sub 发布器
    发布 1s Bar 到 bar1s:{symbol} 通道
    """

    def __init__(self, redis_client: redis.Redis):
        self.redis = redis_client

    async def publish_bar1s(self, bar: Bar1s) -> int:
        """
        发布 1s Bar 到 Redis Pub/Sub

        Args:
            bar: 1s Bar 事件

        Returns:
            订阅者数量
        """
        channel = f"bar1s:{bar.symbol}"

        # 序列化为 JSON
        payload = {
            "symbol": bar.symbol,
            "timestamp": bar.timestamp,
            "available_time": bar.available_time,
            "open": str(bar.open),
            "high": str(bar.high),
            "low": str(bar.low),
            "close": str(bar.close),
            "volume": str(bar.volume),
            "trade_count": bar.trade_count,
            "vwap": str(bar.vwap),
        }

        message = json.dumps(payload, cls=DecimalEncoder)

        try:
            # 发布到 Redis
            subscriber_count = await self.redis.publish(channel, message)
            logger.debug(
                f"发布 Bar1s: {bar.symbol} @ {bar.timestamp}, "
                f"订阅者={subscriber_count}"
            )
            return subscriber_count

        except Exception as e:
            logger.error(f"发布 Bar1s 失败: {e}", exc_info=True)
            raise

    async def close(self) -> None:
        """关闭 Redis 连接"""
        await self.redis.aclose()
