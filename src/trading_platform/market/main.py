"""
行情层主进程
协调 WebSocket 接入、Bar 聚合、Redis 发布和 FastAPI 服务
"""
import asyncio
import logging
import signal
import sys
import time
import uuid
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any

import redis.asyncio as redis
import uvicorn
from fastapi import FastAPI, Response, status

from trading_platform.market.api.routes import (
    HealthResponse,
    SubscriptionManager,
    SubscriptionRequest,
    SubscriptionResponse,
    UnsubscribeResponse,
)
from trading_platform.market.feed.aggregator import Bar1sAggregator
from trading_platform.market.feed.binance_ws import (
    BinanceWebSocketClient,
    build_stream_names,
    parse_aggtrade_message,
    parse_kline_message,
)
from trading_platform.market.store.kline_store import KlineStore
from trading_platform.market.store.redis_pub import RedisPublisher
from trading_platform.shared.config import BinanceConfig, MarketLayerConfig


logger = logging.getLogger(__name__)


class MarketLayerService:
    """
    行情层服务

    职责：
    1. 管理订阅状态（通过 SubscriptionManager）
    2. 根据订阅需求连接 Binance WebSocket
    3. 聚合 aggTrade 为 1s Bar
    4. 发布 Bar1s 到 Redis Pub/Sub
    5. 存储 Kline 到 Redis Hash
    """

    def __init__(
        self,
        config: MarketLayerConfig,
        redis_client: redis.Redis,
        instance_epoch: str,
    ):
        self.config = config
        self.redis = redis_client
        self.instance_epoch = instance_epoch
        self.start_time = time.time()

        # 订阅管理器
        self.subscription_manager = SubscriptionManager(instance_epoch)

        # WebSocket 客户端
        self.ws_client = BinanceWebSocketClient(
            ws_base_url=BinanceConfig().ws_base_url,
            reconnect_delay=5.0,
        )

        # Bar 聚合器
        self.aggregator = Bar1sAggregator(window_tolerance_ms=5000)

        # Redis 发布器和存储
        self.redis_publisher = RedisPublisher(redis_client)
        self.kline_store = KlineStore(redis_client)

        # 运行状态
        self._running = False
        self._ws_task: asyncio.Task | None = None
        self._current_streams: list[str] = []
        self._refresh_lock = asyncio.Lock()

    async def start(self) -> None:
        """启动服务"""
        self._running = True
        logger.info(f"行情层服务启动，instance_epoch={self.instance_epoch}")

    async def stop(self) -> None:
        """停止服务"""
        logger.info("正在停止行情层服务...")
        self._running = False

        async with self._refresh_lock:
            await self._stop_ws_task()
            await self.ws_client.disconnect()
            self._current_streams = []

        logger.info("行情层服务已停止")

    async def refresh_ws_streams(self) -> None:
        """
        根据当前订阅刷新 WebSocket 流

        当订阅变化时调用，重新连接 WebSocket
        """
        async with self._refresh_lock:
            active_streams = self.subscription_manager.get_active_streams()
            needed_streams = sorted(
                {
                    stream
                    for symbol, symbol_types in active_streams.items()
                    for stream in build_stream_names([symbol], list(symbol_types))
                }
            )

            task_is_running = self._ws_task is not None and not self._ws_task.done()
            if needed_streams == self._current_streams and task_is_running:
                logger.debug("WebSocket 流无变化，跳过重连")
                return

            if not needed_streams:
                logger.info("无活跃订阅，关闭 WebSocket")
            elif needed_streams == self._current_streams:
                logger.warning("WebSocket 消息任务已结束，使用原订阅重新启动")
            else:
                logger.info(f"刷新 WebSocket 流: {len(needed_streams)} 个流")

            await self._stop_ws_task()
            await self.ws_client.disconnect()
            self._current_streams = []

            if needed_streams:
                await self.ws_client.connect(needed_streams)
                task = asyncio.create_task(self._ws_message_loop())
                self._ws_task = task
                self._current_streams = needed_streams
                task.add_done_callback(self._on_ws_task_done)

    async def _stop_ws_task(self) -> None:
        task = self._ws_task
        self._ws_task = None
        if task is None:
            return
        if not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.warning(f"回收已失败的 WebSocket 消息任务: {exc}")

    def _on_ws_task_done(self, task: asyncio.Task) -> None:
        if task is not self._ws_task:
            return
        self._ws_task = None
        self._current_streams = []
        if task.cancelled():
            return
        exception = task.exception()
        if exception is not None:
            logger.error(
                f"WebSocket 消息任务异常结束: {exception}",
                exc_info=(type(exception), exception, exception.__traceback__),
            )
        else:
            logger.warning("WebSocket 消息任务意外结束")

    async def _ws_message_loop(self) -> None:
        """WebSocket 消息处理循环"""
        try:
            async for message in self.ws_client.receive_messages():
                await self._handle_ws_message(message)
        except asyncio.CancelledError:
            logger.info("WebSocket 消息循环已取消")
        except Exception as e:
            logger.error(f"WebSocket 消息循环异常: {e}", exc_info=True)

    async def _handle_ws_message(self, message: dict[str, Any]) -> None:
        """处理单条 WebSocket 消息"""
        try:
            # 解析 aggTrade
            aggtrade_result = parse_aggtrade_message(message)
            if aggtrade_result:
                symbol, trade_data = aggtrade_result
                await self._handle_aggtrade(symbol, trade_data)
                return

            # 解析 Kline
            kline_result = parse_kline_message(message)
            if kline_result:
                symbol, interval, kline = kline_result
                await self._handle_kline(symbol, interval, kline)
                return

            # 未识别的消息类型
            logger.debug(f"未识别的消息类型: {message.get('e', 'unknown')}")

        except Exception as e:
            logger.error(f"处理消息失败: {e}", exc_info=True)

    async def _handle_aggtrade(self, symbol: str, trade_data: dict[str, Any]) -> None:
        """处理 aggTrade，聚合为 1s Bar"""
        bars = self.aggregator.add_trade(
            symbol=symbol,
            price=trade_data["price"],
            quantity=trade_data["quantity"],
            timestamp=trade_data["timestamp"],
        )

        for bar in bars:
            await self.redis_publisher.publish_bar1s(bar)

    async def _handle_kline(self, symbol: str, interval: str, kline: Any) -> None:
        """处理已完成的 Kline，存储到 Redis Hash"""
        await self.kline_store.store_kline(kline)

    def get_uptime(self) -> float:
        """获取运行时长（秒）"""
        return time.time() - self.start_time


# ==================== FastAPI 应用 ====================


def create_app(
    config: MarketLayerConfig,
    instance_epoch: str,
) -> tuple[FastAPI, MarketLayerService]:
    """创建 FastAPI 应用和服务实例"""

    # 创建 Redis 客户端
    redis_client = redis.Redis(
        host=config.redis.host,
        port=config.redis.port,
        db=config.redis.db,
        password=config.redis.password,
        decode_responses=False,
    )

    # 创建服务实例
    service = MarketLayerService(
        config=config,
        redis_client=redis_client,
        instance_epoch=instance_epoch,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """FastAPI 生命周期管理"""
        await service.start()
        yield
        await service.stop()

    # 创建 FastAPI 应用
    app = FastAPI(
        title="行情层 API",
        description="Binance WebSocket 接入和 1s Bar 聚合",
        version="1.0.0",
        lifespan=lifespan,
    )

    # 注册订阅路由。这里直接注册带 WS 刷新的处理器，避免与基础 router
    # 注册同一路径后由旧 handler 抢先匹配。
    @app.put("/subscriptions/{consumer_id}")
    async def update_subscription_with_refresh(
        consumer_id: str, request: SubscriptionRequest
    ) -> SubscriptionResponse:
        """扩展订阅接口，在订阅变更后刷新 WebSocket 流"""
        result = service.subscription_manager.update_subscription(
            consumer_id=consumer_id,
            symbols=request.symbols,
            types=request.types,
        )

        # 刷新 WebSocket 流
        await service.refresh_ws_streams()

        return SubscriptionResponse(
            consumer_id=consumer_id,
            subscribed=result["subscribed"],
            active_streams=sum(
                len(types) for types in service.subscription_manager.refcounts.values()
            ),
        )

    @app.delete("/subscriptions/{consumer_id}")
    async def remove_subscription_with_refresh(consumer_id: str) -> UnsubscribeResponse:
        """扩展取消订阅接口，在订阅变更后刷新 WebSocket 流"""
        service.subscription_manager.remove_consumer(consumer_id)

        # 刷新 WebSocket 流
        await service.refresh_ws_streams()

        return UnsubscribeResponse(consumer_id=consumer_id)

    # 更新健康检查，返回运行时长
    @app.get("/health")
    async def health_check(response: Response) -> HealthResponse:
        """健康检查"""
        stats = service.subscription_manager.get_stats()
        try:
            redis_connected = bool(await service.redis.ping())
        except Exception:
            redis_connected = False

        websocket_required = bool(service._current_streams)
        websocket_connected = (
            not websocket_required
            or (
                service._ws_task is not None
                and not service._ws_task.done()
                and service.ws_client.connected
            )
        )
        ready = redis_connected and websocket_connected
        if not ready:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

        return HealthResponse(
            status="ready" if ready else "degraded",
            instance_epoch=stats["instance_epoch"],
            uptime_seconds=service.get_uptime(),
            subscribed_symbols=stats["subscribed_symbols"],
            active_ws_streams=stats["active_streams"],
            redis_connected=redis_connected,
            websocket_connected=websocket_connected,
        )

    return app, service


# ==================== 主函数 ====================


async def main() -> None:
    """主函数"""
    # 配置日志
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    # 生成 instance_epoch
    instance_epoch = str(uuid.uuid4())
    logger.info(f"启动行情层，instance_epoch={instance_epoch}")

    # 加载配置
    config = MarketLayerConfig()

    # 创建应用
    app, service = create_app(config, instance_epoch)

    # 配置 uvicorn
    uv_config = uvicorn.Config(
        app,
        host=config.host,
        port=config.port,
        log_level="info",
        access_log=True,
    )

    server = uvicorn.Server(uv_config)

    # 信号处理
    def signal_handler(signum, frame):
        logger.info(f"收到信号 {signum}，准备关闭...")
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # 启动服务器
    await server.serve()


if __name__ == "__main__":
    asyncio.run(main())
