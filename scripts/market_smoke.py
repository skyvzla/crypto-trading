"""
行情层测试脚本
测试订阅管理、WebSocket 接入和数据聚合
"""
import asyncio
import json
import logging
import os
import time
from uuid import uuid4

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
SMOKE_SYMBOL = os.getenv("MARKET_SMOKE_SYMBOL", "BTCUSDT").upper()
SMOKE_TIMEOUT_SECONDS = int(os.getenv("MARKET_SMOKE_TIMEOUT_SECONDS", "90"))
SMOKE_BAR_COUNT = int(os.getenv("MARKET_SMOKE_BAR_COUNT", "3"))


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
        await pubsub.aclose()
        await redis_client.aclose()


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
        data = None
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
        await redis_client.aclose()


async def test_external_e2e():
    """验证公开 testnet 行情到 Redis 与质量门禁的完整链路。"""
    if SMOKE_TIMEOUT_SECONDS <= 0 or SMOKE_BAR_COUNT <= 0:
        raise ValueError("smoke timeout and bar count must be positive")

    symbol = SMOKE_SYMBOL
    stream_symbol = symbol.lower()
    consumer_id = f"market_smoke_{uuid4().hex[:8]}"
    channel = f"bar1s:{symbol}"
    kline_key = f"kline:{symbol}:1m"
    minimum_close_time = (int(time.time() * 1000) // 60_000) * 60_000 - 1
    deadline = time.monotonic() + SMOKE_TIMEOUT_SECONDS
    bars: list[dict] = []
    completed_kline = None

    redis_client = redis.Redis(
        host=REDIS_HOST,
        port=REDIS_PORT,
        db=0,
        decode_responses=True,
    )
    pubsub = redis_client.pubsub()

    async with httpx.AsyncClient(base_url=MARKET_BASE_URL, timeout=5) as client:
        try:
            health_response = await client.get("/health")
            health_response.raise_for_status()
            if health_response.json().get("binance_testnet") is not True:
                raise RuntimeError("external smoke refuses non-testnet market service")

            await pubsub.subscribe(channel)
            response = await client.put(
                f"/subscriptions/{consumer_id}",
                json={"symbols": [symbol], "types": ["bar1s", "kline:1m"]},
            )
            response.raise_for_status()

            while time.monotonic() < deadline:
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True,
                    timeout=1,
                )
                if message and message["type"] == "message":
                    bars.append(json.loads(message["data"]))

                raw_kline = await redis_client.hget(kline_key, "latest")
                if raw_kline:
                    candidate = json.loads(raw_kline)
                    if int(candidate["close_time"]) >= minimum_close_time:
                        completed_kline = candidate

                if len(bars) >= SMOKE_BAR_COUNT and completed_kline is not None:
                    break

            if len(bars) < SMOKE_BAR_COUNT:
                raise TimeoutError(
                    f"received {len(bars)}/{SMOKE_BAR_COUNT} completed 1s bars"
                )
            if completed_kline is None:
                raise TimeoutError("no fresh completed 1m kline")

            quality_response = await client.get("/quality")
            quality_response.raise_for_status()
            quality = quality_response.json()
            required_streams = {
                f"{stream_symbol}@aggTrade",
                f"{stream_symbol}@kline_1m",
            }
            if not quality.get("ready"):
                raise RuntimeError("market quality is not ready")
            if not required_streams.issubset(quality.get("streams", {})):
                raise RuntimeError("quality response is missing required streams")
            if any(
                quality["streams"][stream]["status"] != "healthy"
                for stream in required_streams
            ):
                raise RuntimeError("one or more market streams are degraded")

            summary = {
                "symbol": symbol,
                "bar_count": len(bars),
                "first_bar_timestamp": bars[0]["timestamp"],
                "last_bar_timestamp": bars[-1]["timestamp"],
                "kline_close_time": completed_kline["close_time"],
                "connection_generation": quality["connection_generation"],
                "quality_ready": quality["ready"],
            }
            print(json.dumps(summary, sort_keys=True))
        finally:
            try:
                await client.delete(f"/subscriptions/{consumer_id}")
            finally:
                await pubsub.unsubscribe(channel)
                await pubsub.aclose()
                await redis_client.aclose()


async def main():
    """主测试流程"""
    import sys

    if len(sys.argv) < 2:
        print("用法: python scripts/market_smoke.py [api|pubsub|kline|e2e]")
        print("  api    - 测试订阅管理 API")
        print("  pubsub - 测试 Redis Pub/Sub 数据接收")
        print("  kline  - 测试 K 线存储")
        print("  e2e    - 验证公开 testnet 行情、Redis 与质量门禁")
        return

    test_type = sys.argv[1]

    if test_type == "api":
        await test_subscription_api()
    elif test_type == "pubsub":
        await test_redis_pubsub()
    elif test_type == "kline":
        await test_kline_storage()
    elif test_type == "e2e":
        await test_external_e2e()
    else:
        print(f"未知测试类型: {test_type}")


if __name__ == "__main__":
    asyncio.run(main())
