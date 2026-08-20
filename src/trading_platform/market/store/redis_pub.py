"""
Redis Pub/Sub 发布器
发布 1s Bar 到 bar1s:{symbol} 通道
"""
import json
import logging
import time
from dataclasses import dataclass

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


class RedisPublisher:
    """
    Redis Pub/Sub 发布器
    发布 1s Bar 到 bar1s:{symbol} 通道
    """

    STREAM_MAXLEN = 900

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

        # None 表示上游数据源不提供该维度；省略可保持旧消费者兼容并避免
        # 订单流扩展字段在不可用时无意义地放大 Redis 消息。
        payload = {key: value for key, value in bar.to_dict().items() if value is not None}
        if not bar.orderflow_available:
            # 旧的测试/回补调用可能只有 price/quantity。此时保持原有 wire
            # schema；真实 Binance aggTrade 带 is_buyer_maker，会走扩展 schema。
            for key in (
                "quote_volume",
                "raw_trade_count",
                "taker_buy_volume",
                "taker_sell_volume",
                "taker_buy_quote_volume",
                "taker_sell_quote_volume",
                "taker_buy_trade_count",
                "taker_sell_trade_count",
                "taker_buy_agg_trade_count",
                "taker_sell_agg_trade_count",
                "max_agg_trade_quantity",
                "max_taker_buy_agg_trade_quantity",
                "max_taker_sell_agg_trade_quantity",
                "first_trade_id",
                "last_trade_id",
            ):
                payload.pop(key, None)
        payload.pop("type_priority", None)
        payload.pop("sequence", None)
        message = json.dumps(payload)
        stream = f"bar1s:stream:{bar.symbol}"

        try:
            # Stream 是短期传输日志；必须先落盘，Pub/Sub 消费者才能可靠回放。
            await self.redis.xadd(
                stream,
                {"data": message},
                maxlen=self.STREAM_MAXLEN,
                approximate=True,
            )
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
