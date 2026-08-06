"""
Redis Pub/Sub 发布器
发布 1s Bar 到 bar1s:{symbol} 通道
"""
import json
import logging
import time
from dataclasses import dataclass
from decimal import Decimal

import redis.asyncio as redis

from trading_platform.shared.events import Bar1s


logger = logging.getLogger(__name__)


@dataclass
class ChannelDeliveryState:
    """可观测的 Pub/Sub 交付事实，不对消费者做身份或租约推断。"""

    channel: str
    publish_count: int = 0
    zero_subscriber_count: int = 0
    last_subscriber_count: int | None = None
    last_published_at_ms: int | None = None

    @property
    def status(self) -> str:
        if self.last_subscriber_count is None:
            return "awaiting_publish"
        return "healthy" if self.last_subscriber_count > 0 else "degraded"

    def to_dict(self) -> dict[str, int | str | None]:
        return {
            "channel": self.channel,
            "status": self.status,
            "publish_count": self.publish_count,
            "zero_subscriber_count": self.zero_subscriber_count,
            "last_subscriber_count": self.last_subscriber_count,
            "last_published_at_ms": self.last_published_at_ms,
        }


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
        self._delivery: dict[str, ChannelDeliveryState] = {}

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
            state = self._delivery.setdefault(
                channel, ChannelDeliveryState(channel=channel)
            )
            previous_status = state.status
            state.publish_count += 1
            state.last_subscriber_count = subscriber_count
            state.last_published_at_ms = int(time.time() * 1000)
            if subscriber_count == 0:
                state.zero_subscriber_count += 1
                if previous_status != "degraded":
                    logger.warning(
                        "Redis Pub/Sub 消费端断流: channel=%s, subscriber_count=0",
                        channel,
                    )
            elif previous_status == "degraded":
                logger.info(
                    "Redis Pub/Sub 消费端恢复: channel=%s, subscriber_count=%s",
                    channel,
                    subscriber_count,
                )
            logger.debug(
                f"发布 Bar1s: {bar.symbol} @ {bar.timestamp}, "
                f"订阅者={subscriber_count}"
            )
            return subscriber_count

        except Exception as e:
            logger.error(f"发布 Bar1s 失败: {e}", exc_info=True)
            raise

    @property
    def delivery_ready(self) -> bool:
        """所有已发布通道最近一次均有消费者；尚未发布的通道不阻塞准入。"""
        return all(state.status != "degraded" for state in self._delivery.values())

    @property
    def delivery_issue_count(self) -> int:
        return sum(state.status == "degraded" for state in self._delivery.values())

    def delivery_snapshot(self) -> dict[str, dict[str, int | str | None]]:
        return {
            channel: self._delivery[channel].to_dict()
            for channel in sorted(self._delivery)
        }

    async def close(self) -> None:
        """关闭 Redis 连接"""
        await self.redis.aclose()
