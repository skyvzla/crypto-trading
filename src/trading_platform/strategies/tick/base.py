"""
1s 事件策略基类
Redis Pub/Sub 驱动，订阅行情层推送的 1s Bar
"""
import asyncio
import json
import logging
from abc import ABC, abstractmethod
from decimal import Decimal
from typing import Any

import httpx
import redis.asyncio as aioredis

from trading_platform.shared.events import Bar1s, OrderIntent
from trading_platform.shared.binance import BinanceRestClient, UserDataStream
from trading_platform.shared.config import BinanceConfig, RedisConfig, StrategyConfig

logger = logging.getLogger(__name__)


class TickStrategyBase(ABC):
    """
    1s 事件策略基类

    职责：
    - 订阅行情层 Redis Pub/Sub
    - 接收 1s Bar 事件
    - 调用子类的 on_bar1s() 方法
    - 健康检查循环（检测行情层重启）
    - 订阅注册和恢复
    """

    def __init__(
        self,
        strategy_name: str,
        consumer_id: str,
        symbols: list[str],
        account_id: str,
        binance_config: BinanceConfig,
        redis_config: RedisConfig,
        strategy_config: StrategyConfig,
    ):
        """
        Args:
            strategy_name: 策略名称
            consumer_id: 消费者ID（格式：tick_strategy_{name}_{instance_id}）
            symbols: 订阅的交易对列表
            account_id: 账户ID
            binance_config: Binance 配置
            redis_config: Redis 配置
            strategy_config: 策略配置
        """
        self.strategy_name = strategy_name
        self.consumer_id = consumer_id
        self.symbols = symbols
        self.account_id = account_id
        self.binance_config = binance_config
        self.redis_config = redis_config
        self.strategy_config = strategy_config

        # Redis 客户端
        self.redis: aioredis.Redis | None = None
        self.pubsub: aioredis.client.PubSub | None = None

        # Binance 客户端
        self.rest_client: BinanceRestClient | None = None
        self.user_stream: UserDataStream | None = None

        # HTTP 客户端（用于行情层 API 调用）
        self.http_client: httpx.AsyncClient | None = None

        # 行情层状态
        self.last_known_epoch: str | None = None

        # 运行状态
        self._running = False
        self._health_check_task: asyncio.Task | None = None

    async def start(self) -> None:
        """启动策略"""
        if self._running:
            logger.warning(f"Strategy {self.strategy_name} already running")
            return

        logger.info(f"Starting strategy {self.strategy_name}...")

        # 初始化 Redis
        self.redis = aioredis.Redis(
            host=self.redis_config.host,
            port=self.redis_config.port,
            db=self.redis_config.db,
            password=self.redis_config.password,
            decode_responses=True,
        )

        # 初始化 HTTP 客户端
        self.http_client = httpx.AsyncClient(
            base_url=self.strategy_config.market_api_url,
            timeout=10.0,
        )

        # 初始化 Binance 客户端
        self.rest_client = BinanceRestClient(
            api_key=self.binance_config.api_key,
            api_secret=self.binance_config.api_secret,
            base_url=self.binance_config.base_url,
        )

        # 初始化 User Data Stream
        self.user_stream = UserDataStream(
            rest_client=self.rest_client,
            ws_base_url=self.binance_config.ws_base_url,
            on_execution_report=self._on_execution_report,
            on_reconnect=self._on_user_stream_reconnect,
        )

        # 等待行情层就绪
        await self._wait_for_market_layer()

        # 注册订阅
        await self._register_subscriptions()

        # 启动 User Data Stream
        await self.user_stream.start()

        # 启动 Redis Pub/Sub
        await self._start_pubsub()

        # 启动健康检查循环
        self._health_check_task = asyncio.create_task(self._health_check_loop())

        self._running = True
        logger.info(f"Strategy {self.strategy_name} started")

    async def stop(self) -> None:
        """停止策略"""
        if not self._running:
            return

        logger.info(f"Stopping strategy {self.strategy_name}...")
        self._running = False

        # 停止健康检查
        if self._health_check_task:
            self._health_check_task.cancel()
            try:
                await self._health_check_task
            except asyncio.CancelledError:
                pass

        # 注销订阅
        await self._unregister_subscriptions()

        # 关闭 User Data Stream
        if self.user_stream:
            await self.user_stream.stop()

        # 关闭 Pub/Sub
        if self.pubsub:
            await self.pubsub.unsubscribe()
            await self.pubsub.close()

        # 关闭 Redis
        if self.redis:
            await self.redis.close()

        # 关闭 Binance 客户端
        if self.rest_client:
            await self.rest_client.close()

        # 关闭 HTTP 客户端
        if self.http_client:
            await self.http_client.aclose()

        logger.info(f"Strategy {self.strategy_name} stopped")

    async def _wait_for_market_layer(self) -> None:
        """等待行情层就绪"""
        logger.info("Waiting for market layer...")
        max_attempts = 30
        for attempt in range(max_attempts):
            try:
                response = await self.http_client.get("/health")
                if response.status_code == 200:
                    health = response.json()
                    self.last_known_epoch = health.get("instance_epoch")
                    logger.info(f"Market layer ready, epoch: {self.last_known_epoch}")
                    return
            except Exception as e:
                logger.warning(f"Market layer not ready (attempt {attempt+1}/{max_attempts}): {e}")
                await asyncio.sleep(2)

        raise RuntimeError("Market layer not available after 30 attempts")

    async def _register_subscriptions(self) -> None:
        """注册订阅（声明式幂等接口）"""
        payload = {
            "symbols": self.symbols,
            "types": ["bar1s"],  # 1s 事件策略只订阅 bar1s
        }

        try:
            response = await self.http_client.put(
                f"/subscriptions/{self.consumer_id}",
                json=payload,
            )
            if response.status_code == 200:
                result = response.json()
                logger.info(f"Subscriptions registered: {result}")
            else:
                logger.error(f"Failed to register subscriptions: {response.status_code} {response.text}")
                raise RuntimeError("Subscription registration failed")
        except Exception as e:
            logger.error(f"Error registering subscriptions: {e}")
            raise

    async def _unregister_subscriptions(self) -> None:
        """注销订阅"""
        try:
            response = await self.http_client.delete(f"/subscriptions/{self.consumer_id}")
            if response.status_code == 200:
                logger.info("Subscriptions unregistered")
            else:
                logger.warning(f"Failed to unregister subscriptions: {response.status_code}")
        except Exception as e:
            logger.error(f"Error unregistering subscriptions: {e}")

    async def _start_pubsub(self) -> None:
        """启动 Redis Pub/Sub"""
        self.pubsub = self.redis.pubsub()

        # 订阅所有交易对的 bar1s 通道
        channels = [f"bar1s:{symbol}" for symbol in self.symbols]
        await self.pubsub.subscribe(*channels)

        # 启动消息处理循环
        asyncio.create_task(self._pubsub_loop())
        logger.info(f"Subscribed to channels: {channels}")

    async def _pubsub_loop(self) -> None:
        """Pub/Sub 消息循环"""
        async for message in self.pubsub.listen():
            if not self._running:
                break

            if message['type'] == 'message':
                channel = message['channel']
                data = message['data']

                try:
                    # 解析 Bar1s
                    bar = Bar1s.from_json(data)

                    # 调用子类处理方法
                    await self.on_bar1s(bar)

                except Exception as e:
                    logger.error(f"Error processing bar1s from {channel}: {e}", exc_info=True)

    async def _health_check_loop(self) -> None:
        """健康检查循环（每30秒检测行情层重启）"""
        while self._running:
            await asyncio.sleep(30)

            try:
                response = await self.http_client.get("/health")
                if response.status_code == 200:
                    health = response.json()
                    current_epoch = health.get("instance_epoch")

                    if self.last_known_epoch and current_epoch != self.last_known_epoch:
                        logger.warning(f"Market layer restarted (epoch changed), re-registering subscriptions")
                        await self._register_subscriptions()

                    self.last_known_epoch = current_epoch
            except Exception as e:
                logger.error(f"Health check failed: {e}")

    async def _on_execution_report(self, order_data: dict[str, Any]) -> None:
        """
        User Data Stream executionReport 回调

        子类可以重写此方法来处理订单更新
        """
        client_order_id = order_data.get('c')
        status = order_data.get('X')
        logger.info(f"Order update: {client_order_id} -> {status}")

    async def _on_user_stream_reconnect(self) -> None:
        """
        User Data Stream 重连回调

        触发启动对账逻辑（在此基类中提供空实现，子类可重写）
        """
        logger.warning("User Data Stream reconnected, should run reconciliation")

    @abstractmethod
    async def on_bar1s(self, bar: Bar1s) -> None:
        """
        处理 1s Bar 事件（子类必须实现）

        Args:
            bar: 1s Bar 数据
        """
        pass
