"""
K线存储
写入 Redis Hash kline:{symbol}:{interval} 的 latest 字段
只保留最新完成的 K 线
"""
import json
import logging
from decimal import Decimal

import redis.asyncio as redis

from trading_platform.shared.events import Kline


logger = logging.getLogger(__name__)


class DecimalEncoder(json.JSONEncoder):
    """处理 Decimal 类型的 JSON 编码器"""

    def default(self, obj):
        if isinstance(obj, Decimal):
            return str(obj)
        return super().default(obj)


class KlineStore:
    """
    K线存储
    写入 Redis Hash kline:{symbol}:{interval} 的 latest 字段
    只保留最新完成的 K 线
    """

    def __init__(self, redis_client: redis.Redis):
        self.redis = redis_client

    async def store_kline(self, kline: Kline) -> None:
        """
        存储 K 线到 Redis Hash

        Key: kline:{symbol}:{interval}
        Field: latest
        Value: JSON 序列化的 K 线数据

        Args:
            kline: K 线事件
        """
        key = f"kline:{kline.symbol}:{kline.interval}"

        # 序列化为 JSON
        payload = {
            "symbol": kline.symbol,
            "interval": kline.interval,
            "open_time": kline.open_time,
            "close_time": kline.close_time,
            "available_time": kline.available_time,
            "open": str(kline.open),
            "high": str(kline.high),
            "low": str(kline.low),
            "close": str(kline.close),
            "volume": str(kline.volume),
        }

        message = json.dumps(payload, cls=DecimalEncoder)

        try:
            # 写入 Redis Hash 的 latest 字段
            await self.redis.hset(key, "latest", message)

            logger.debug(
                f"存储 Kline: {kline.symbol} {kline.interval} "
                f"close_time={kline.close_time}"
            )

        except Exception as e:
            logger.error(f"存储 Kline 失败: {e}", exc_info=True)
            raise

    async def get_latest_kline(self, symbol: str, interval: str) -> dict | None:
        """
        获取最新的 K 线

        Args:
            symbol: 交易对
            interval: K 线周期

        Returns:
            K 线数据字典或 None
        """
        key = f"kline:{symbol}:{interval}"

        try:
            data = await self.redis.hget(key, "latest")

            if data is None:
                return None

            return json.loads(data)

        except Exception as e:
            logger.error(f"读取 Kline 失败: {e}", exc_info=True)
            return None

    async def clear_symbol(self, symbol: str) -> int:
        """
        清除某个交易对的所有 K 线数据

        Args:
            symbol: 交易对

        Returns:
            删除的 key 数量
        """
        pattern = f"kline:{symbol}:*"

        try:
            # 查找匹配的 key
            keys = []
            async for key in self.redis.scan_iter(match=pattern):
                keys.append(key)

            if not keys:
                return 0

            # 删除
            count = await self.redis.delete(*keys)
            logger.info(f"清除 {symbol} 的 {count} 个 K 线 key")
            return count

        except Exception as e:
            logger.error(f"清除 Kline 失败: {e}", exc_info=True)
            return 0

    async def close(self) -> None:
        """关闭 Redis 连接"""
        await self.redis.close()
