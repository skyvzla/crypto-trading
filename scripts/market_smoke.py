"""
行情层测试脚本
测试订阅管理、WebSocket 接入和数据聚合
"""
import asyncio
import json
import logging
import os

import httpx
import redis.asyncio as redis


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

MARKET_BASE_URL = os.getenv("MARKET_BASE_URL", "http://market:8000")
REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))


async def test_subscription_api():
    """测试订阅管理 API"""
    logger.info("=" * 60)
    logger.info("测试订阅管理 API")
    logger.info("=" * 60)

    base_url = MARKET_BASE_URL

    async with httpx.AsyncClient() as client:
        # 1. 健康检查
        logger.info("\n1. 健康检查")
        response = await client.get(f"{base_url}/health")
        health = response.json()
        logger.info(f"健康状态: {json.dumps(health, indent=2)}")

        instance_epoch = health["instance_epoch"]
        logger.info(f"instance_epoch: {instance_epoch}")

        # 2. 订阅交易对
        logger.info("\n2. 订阅交易对")
        consumer_id = "test_strategy_001"
        subscription = {
            "symbols": ["BTCUSDT", "ETHUSDT"],
            "types": ["bar1s", "kline:1m", "kline:5m"],
        }

        response = await client.put(
            f"{base_url}/subscriptions/{consumer_id}",
            json=subscription,
        )
        result = response.json()
        logger.info(f"订阅结果: {json.dumps(result, indent=2)}")

        # 3. 再次健康检查，确认订阅生效
        logger.info("\n3. 确认订阅生效")
        await asyncio.sleep(1)
        response = await client.get(f"{base_url}/health")
        health = response.json()
        logger.info(f"订阅后状态: {json.dumps(health, indent=2)}")

        # 4. 更新订阅（减少交易对）
        logger.info("\n4. 更新订阅")
        subscription = {
            "symbols": ["BTCUSDT"],
            "types": ["bar1s"],
        }

        response = await client.put(
            f"{base_url}/subscriptions/{consumer_id}",
            json=subscription,
        )
        result = response.json()
        logger.info(f"更新结果: {json.dumps(result, indent=2)}")

        # 5. 取消订阅
        logger.info("\n5. 取消订阅")
        response = await client.delete(f"{base_url}/subscriptions/{consumer_id}")
        result = response.json()
        logger.info(f"取消结果: {json.dumps(result, indent=2)}")

        # 6. 最终健康检查
        logger.info("\n6. 最终状态")
        response = await client.get(f"{base_url}/health")
        health = response.json()
        logger.info(f"最终状态: {json.dumps(health, indent=2)}")


async def test_redis_pubsub():
    """测试 Redis Pub/Sub 数据接收"""
    logger.info("=" * 60)
    logger.info("测试 Redis Pub/Sub 数据接收")
    logger.info("=" * 60)

    # 连接 Redis
    redis_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0, decode_responses=True)

    # 订阅 BTCUSDT 的 bar1s 通道
    pubsub = redis_client.pubsub()
    await pubsub.subscribe("bar1s:BTCUSDT")

    logger.info("已订阅 bar1s:BTCUSDT，等待数据...")

    # 接收前 10 条消息
    message_count = 0
    max_messages = 10

    try:
        async for message in pubsub.listen():
            if message["type"] == "message":
                data = json.loads(message["data"])
                logger.info(f"收到 Bar1s: {data['symbol']} @ {data['timestamp']}, close={data['close']}")

                message_count += 1
                if message_count >= max_messages:
                    logger.info(f"已接收 {max_messages} 条消息，测试完成")
                    break

    except KeyboardInterrupt:
        logger.info("测试中断")

    finally:
        await pubsub.unsubscribe("bar1s:BTCUSDT")
        await redis_client.close()


async def test_kline_storage():
    """测试 K 线存储"""
    logger.info("=" * 60)
    logger.info("测试 K 线存储")
    logger.info("=" * 60)

    # 连接 Redis
    redis_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0, decode_responses=True)

    symbol = "BTCUSDT"
    interval = "1m"

    try:
        # 等待 K 线数据
        logger.info(f"等待 {symbol} {interval} K 线数据...")
        for i in range(60):
            data = await redis_client.hget(f"kline:{symbol}:{interval}", "latest")

            if data:
                kline = json.loads(data)
                logger.info(f"读取到 K 线: {json.dumps(kline, indent=2)}")
                break

            await asyncio.sleep(1)

        if not data:
            logger.warning("60 秒内未收到 K 线数据")

    finally:
        await redis_client.close()


async def main():
    """主测试流程"""
    import sys

    if len(sys.argv) < 2:
        print("用法: python scripts/market_smoke.py [api|pubsub|kline]")
        print("  api    - 测试订阅管理 API")
        print("  pubsub - 测试 Redis Pub/Sub 数据接收")
        print("  kline  - 测试 K 线存储")
        return

    test_type = sys.argv[1]

    if test_type == "api":
        await test_subscription_api()
    elif test_type == "pubsub":
        await test_redis_pubsub()
    elif test_type == "kline":
        await test_kline_storage()
    else:
        print(f"未知测试类型: {test_type}")


if __name__ == "__main__":
    asyncio.run(main())
