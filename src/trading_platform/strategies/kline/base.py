"""
K线策略基类
asyncio 定时器驱动，从 Redis Hash 读取最新完成的 K 线
"""
import asyncio
import json
import logging
from abc import ABC, abstractmethod
from decimal import Decimal
from typing import Any

import httpx
import redis.asyncio as aioredis

from trading_platform.shared.events import Kline, OrderIntent
from trading_platform.shared.binance import BinanceRestClient, UserDataStream
from trading_platform.shared.config import BinanceConfig, RedisConfig, StrategyConfig

logger = logging.getLogger(__name__)


class KlineStrategyBase(ABC):
    """
    K 线策略基类

    职责：
    - 定时器驱动，每个周期触发一次
    - 从 Redis Hash 读取最新完成的 K 线
    - 去重机制（避免重复处理同一根 K 线）
    - 调用子类的 on_timer() / on_kline() 方法
    - 健康检查循环（检测行情层重启）
    - 订阅注册和恢复
    """

    def __init__(
        self,
        strategy_name: str,
        consumer_id: str,
        symbols: list[str],
        intervals: list[str],  # ['1m', '5m', '15m', '1h']
        account_id: str,
        binance_config: BinanceConfig,
        redis_config: RedisConfig,
        strategy_config: StrategyConfig,
    ):
        """
        Args:
            strategy_name: 策略名称
            consumer_id: 消费者ID（格式：kline_strategy_{name}_{instance_id}）
            symbols: 订阅的交易对列表
            intervals: K 线周期列表
            account_id: 账户ID
            binance_config: Binance 配置
            redis_config: Redis 配置
            strategy_config: 策略配置
        """
        self.strategy_name = strategy_name
        self.consumer_id = consumer_id
        self.symbols = symbols
        self.intervals = intervals
        self.account_id = account_id
        self.binance_config = binance_config
        self.redis_config = redis_config
        self.strategy_config = strategy_config

        # Redis 客户端
        self.redis: aioredis.Redis | None = None

        # Binance 客户端
        self.rest_client: BinanceRestClient | None = None
        self.user_stream: UserDataStream | None = None

        # HTTP 客户端（用于行情层 API 调用）
        self.http_client: httpx.AsyncClient | None = None

        # 行情层状态
        self.last_known_epoch: str | None = None
        self.market_data_ready = False

        # 去重水位：(symbol, interval) -> last_close_time
        self.last_processed: dict[tuple[str, str], int] = {}

        # 定时器任务
        self._timer_tasks: list[asyncio.Task] = []
        self._health_check_task: asyncio.Task | None = None

        # 运行状态
        self._running = False

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
        # 注册后行情流会短暂处于 awaiting_data，禁止消费 Redis 中的旧快照。
        self.market_data_ready = False

        # 启动 User Data Stream
        await self.user_stream.start()

        self._running = True

        # 启动定时器
        await self._start_timers()

        # 启动健康检查循环
        self._health_check_task = asyncio.create_task(self._health_check_loop())

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

        # 停止定时器
        for task in self._timer_tasks:
            task.cancel()
        await asyncio.gather(*self._timer_tasks, return_exceptions=True)

        # 注销订阅
        await self._unregister_subscriptions()

        # 关闭 User Data Stream
        if self.user_stream:
            await self.user_stream.stop()

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
                    quality = await self.http_client.get("/quality")
                    if quality.status_code == 200 and quality.json().get("ready", False):
                        self.market_data_ready = True
                        logger.info(f"Market layer ready, epoch: {self.last_known_epoch}")
                        return
                    self.market_data_ready = False
            except Exception as e:
                logger.warning(f"Market layer not ready (attempt {attempt+1}/{max_attempts}): {e}")
                await asyncio.sleep(2)

        raise RuntimeError("Market layer not available after 30 attempts")

    async def _register_subscriptions(self) -> None:
        """注册订阅（声明式幂等接口）"""
        types = [f"kline:{interval}" for interval in self.intervals]
        payload = {
            "symbols": self.symbols,
            "types": types,
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

    async def _start_timers(self) -> None:
        """启动定时器（每个周期一个）"""
        interval_seconds = {
            '1m': 60,
            '5m': 300,
            '15m': 900,
            '1h': 3600,
            '4h': 14400,
            '1d': 86400,
        }

        for interval in self.intervals:
            seconds = interval_seconds.get(interval)
            if not seconds:
                logger.error(f"Unknown interval: {interval}")
                continue

            task = asyncio.create_task(self._timer_loop(interval, seconds))
            self._timer_tasks.append(task)
            logger.info(f"Started timer for {interval} ({seconds}s)")

    async def _timer_loop(self, interval: str, seconds: int) -> None:
        """
        定时器循环

        Args:
            interval: K 线周期
            seconds: 定时间隔（秒）
        """
        while self._running:
            await asyncio.sleep(seconds)

            try:
                # 调用子类的 on_timer 方法
                await self.on_timer(interval)

                # 读取所有交易对的最新 K 线
                for symbol in self.symbols:
                    await self._fetch_and_process_kline(symbol, interval)

            except Exception as e:
                logger.error(f"Error in timer loop ({interval}): {e}", exc_info=True)

    async def _fetch_and_process_kline(self, symbol: str, interval: str) -> None:
        """
        从 Redis 读取最新 K 线并处理

        Args:
            symbol: 交易对
            interval: K 线周期
        """
        key = (symbol, interval)
        redis_key = f"kline:{symbol}:{interval}"

        try:
            if not self.market_data_ready:
                return
            # 从 Redis Hash 读取 latest
            kline_json = await self.redis.hget(redis_key, "latest")
            if not kline_json:
                logger.debug(f"No kline data for {redis_key}")
                return

            # 解析 K 线
            kline = Kline.from_json(kline_json)

            # 去重检查
            if key in self.last_processed and kline.close_time <= self.last_processed[key]:
                logger.debug(f"Kline already processed: {symbol} {interval} {kline.close_time}")
                return

            # 处理新 K 线
            try:
                await self.on_kline(kline)

                # 成功处理后才更新水位
                self.last_processed[key] = kline.close_time
                logger.debug(f"Processed kline: {symbol} {interval} {kline.close_time}")

            except Exception as e:
                logger.error(f"Error processing kline {symbol} {interval}: {e}", exc_info=True)
                # 不更新水位，下次定时器会重试

        except Exception as e:
            logger.error(f"Error fetching kline {redis_key}: {e}", exc_info=True)

    async def _health_check_loop(self) -> None:
        """健康检查循环（每30秒检测行情层重启）"""
        while self._running:
            await asyncio.sleep(30)

            try:
                response = await self.http_client.get("/health")
                if response.status_code == 200:
                    health = response.json()
                    current_epoch = health.get("instance_epoch")

                    quality = await self.http_client.get("/quality")
                    self.market_data_ready = (
                        quality.status_code == 200
                        and quality.json().get("ready", False)
                    )

                    if self.last_known_epoch and current_epoch != self.last_known_epoch:
                        logger.warning(f"Market layer restarted (epoch changed), re-registering subscriptions")
                        await self._register_subscriptions()
                        self.market_data_ready = False

                    self.last_known_epoch = current_epoch
                else:
                    self.market_data_ready = False
            except Exception as e:
                self.market_data_ready = False
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
    async def on_timer(self, interval: str) -> None:
        """
        定时器触发回调（子类可选实现）

        在读取 K 线之前调用，可用于清理状态、准备数据等

        Args:
            interval: 触发的 K 线周期
        """
        pass

    @abstractmethod
    async def on_kline(self, kline: Kline) -> None:
        """
        处理 K 线事件（子类必须实现）

        Args:
            kline: K 线数据
        """
        pass
