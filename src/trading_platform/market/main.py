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
from fastapi import FastAPI, HTTPException, Query, Response, status

from trading_platform.market.api.routes import (
    Bar1sRecoveryResponse,
    HealthResponse,
    KlineRangeResponse,
    QualityResponse,
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
from trading_platform.market.quality import MarketDataQualityTracker
from trading_platform.market.recovery import RecoveryError, RecoveryCoordinator
from trading_platform.market.store.kline_store import KlineStore
from trading_platform.market.store.redis_pub import RedisPublisher
from trading_platform.shared.config import BinanceConfig, MarketLayerConfig
from trading_platform.shared.binance.rest_client import BinanceRestClient


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
    WATERMARK_KEY = "market:continuity_watermarks:v2"
    BACKFILL_TIMEOUT_SECONDS = 30.0

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
        self.binance_config = BinanceConfig()
        self.rest_client = BinanceRestClient(
            self.binance_config.api_key, self.binance_config.api_secret,
            base_url=self.binance_config.base_url,
        )
        self.recovery = RecoveryCoordinator()

        # 订阅管理器
        self.subscription_manager = SubscriptionManager(instance_epoch)

        # WebSocket 客户端
        self.ws_client = BinanceWebSocketClient(
            ws_base_url=self.binance_config.ws_base_url,
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
        self._recovery_task: asyncio.Task | None = None
        self.quality = MarketDataQualityTracker()
        self._quality_generation = 0
        self._replay_watermarks: dict[str, int] = {}
        self._backfill_lock = asyncio.Lock()

    async def start(self) -> None:
        """启动服务"""
        self._running = True
        logger.info(f"行情层服务启动，instance_epoch={self.instance_epoch}")

    async def stop(self) -> None:
        """停止服务"""
        logger.info("正在停止行情层服务...")
        self._running = False

        recovery_task = self._recovery_task
        self._recovery_task = None
        if recovery_task is not None and recovery_task is not asyncio.current_task():
            recovery_task.cancel()
            try:
                await recovery_task
            except asyncio.CancelledError:
                pass

        async with self._refresh_lock:
            try:
                await self._stop_ws_task()
                await self.ws_client.disconnect()
            finally:
                self._current_streams = []
                await self.rest_client.close()
                await self.redis.aclose()

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
            self.quality.set_expected_streams(needed_streams)
            removed_streams = set(self._current_streams) - set(needed_streams)
            for stream in removed_streams:
                if "@aggTrade" in stream:
                    self.aggregator.flush_symbol(stream.split("@", 1)[0].upper())
            self._replay_watermarks = {
                stream: value for stream, value in self._replay_watermarks.items()
                if stream in needed_streams
            }

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
                try:
                    await self.ws_client.connect(needed_streams)
                    await self._restore_persisted_watermarks(needed_streams)
                except Exception:
                    await self.ws_client.disconnect()
                    self._schedule_ws_recovery()
                    raise
                self.quality.begin_connection(
                    needed_streams,
                    self.ws_client.connection_generation,
                )
                task = asyncio.create_task(self._ws_message_loop())
                self._ws_task = task
                self._current_streams = needed_streams
                task.add_done_callback(self._on_ws_task_done)

    async def _restore_persisted_watermarks(self, streams: list[str]) -> None:
        values = await self.redis.hgetall(self.WATERMARK_KEY)
        if not isinstance(values, dict):
            return
        decoded = {
            (key.decode() if isinstance(key, bytes) else str(key)):
            (value.decode() if isinstance(value, bytes) else str(value))
            for key, value in values.items()
        }
        for stream in streams:
            value = decoded.get(stream)
            if value is None:
                continue
            if "@aggTrade" in stream:
                self.quality.restore_watermark(stream, last_sequence=int(value))
            elif "@kline_" in stream:
                self.quality.restore_watermark(stream, last_kline_close_time=int(value))

    async def _persist_watermark(self, stream: str, value: int) -> None:
        await self.redis.hset(self.WATERMARK_KEY, stream, str(value))

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
        self._schedule_ws_recovery()

    def _schedule_ws_recovery(self) -> None:
        if not self._running or not self.subscription_manager.get_active_streams():
            return
        if self._recovery_task is not None and not self._recovery_task.done():
            return
        self._recovery_task = asyncio.create_task(self._recover_ws_loop())

    async def _recover_ws_loop(self) -> None:
        try:
            while self._running and self.subscription_manager.get_active_streams():
                await asyncio.sleep(self.ws_client.reconnect_delay)
                try:
                    await self.refresh_ws_streams()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning(f"WebSocket 后台恢复失败: {exc}")
                    continue
                if self._ws_task is not None and not self._ws_task.done():
                    return
        finally:
            if self._recovery_task is asyncio.current_task():
                self._recovery_task = None

    async def _ws_message_loop(self) -> None:
        """WebSocket 消息处理循环"""
        try:
            async for message in self.ws_client.receive_messages():
                await self._recover_if_generation_changed(message)
                await self._handle_ws_message(message)
        except asyncio.CancelledError:
            logger.info("WebSocket 消息循环已取消")
        except Exception as e:
            logger.error(f"WebSocket 消息循环异常: {e}", exc_info=True)

    async def _handle_ws_message(self, message: dict[str, Any]) -> None:
        """处理单条 WebSocket 消息"""
        try:
            if self._is_replayed_overlap(message):
                return
            # 解析 aggTrade
            aggtrade_result = parse_aggtrade_message(message)
            if aggtrade_result:
                symbol, trade_data = aggtrade_result
                await self._handle_aggtrade(symbol, trade_data)
                return

            if message.get("e") == "kline" and not message.get("k", {}).get("x"):
                kline = message["k"]
                self.quality.observe_kline_update(
                    symbol=message["s"],
                    interval=kline["i"],
                    event_time_ms=int(message["E"]),
                    received_at_ms=int(time.time() * 1000),
                )
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
            stream = self._message_stream(message)
            if stream is not None:
                self.quality.mark_stream_failed(stream, "processing_failed")
            logger.error(f"处理消息失败: {e}", exc_info=True)

    @staticmethod
    def _message_stream(message: dict[str, Any]) -> str | None:
        symbol = str(message.get("s", "")).lower()
        if message.get("e") == "aggTrade" and symbol:
            return f"{symbol}@aggTrade"
        if message.get("e") == "kline" and symbol and message.get("k", {}).get("i"):
            return f"{symbol}@kline_{message['k']['i']}"
        return None

    def _is_replayed_overlap(self, message: dict[str, Any]) -> bool:
        """Drop WS events already replayed from REST during this generation."""
        if message.get("e") == "aggTrade":
            stream = f"{message.get('s', '').lower()}@aggTrade"
            sequence = int(message["a"])
        elif message.get("e") == "kline" and message.get("k", {}).get("x"):
            kline = message["k"]
            stream = f"{message.get('s', '').lower()}@kline_{kline['i']}"
            sequence = int(kline["T"])
        else:
            return False
        watermark = self._replay_watermarks.get(stream)
        if watermark is None:
            return False
        if sequence <= watermark:
            return True
        self._replay_watermarks.pop(stream, None)
        return False

    async def _recover_if_generation_changed(
        self, pending_message: dict[str, Any] | None = None
    ) -> None:
        """Backfill continuity gaps before delivering post-reconnect messages."""
        generation = self.ws_client.connection_generation
        if generation == self._quality_generation or not self._current_streams:
            return
        async with self._backfill_lock:
            generation = self.ws_client.connection_generation
            if generation == self._quality_generation or not self._current_streams:
                return
            try:
                async with asyncio.timeout(self.BACKFILL_TIMEOUT_SECONDS):
                    await self._recover_generation(generation, pending_message)
            except TimeoutError:
                self.quality.mark_backfill_failed(list(self._current_streams))
                self._quality_generation = generation
                logger.error("行情断流回补超时，保持 fail-closed")

    async def _recover_generation(
        self, generation: int, pending_message: dict[str, Any] | None = None
    ) -> None:
        streams = list(self._current_streams)
        watermarks = self.quality.watermarks()
        self.quality.prepare_backfill(streams)
        try:
            now_ms = int(time.time() * 1000)
            agg_batches: list[tuple[str, list[dict[str, Any]]]] = []
            kline_batches: list[tuple[str, str, list[Any]]] = []
            for stream in streams:
                if "@aggTrade" in stream:
                    symbol = stream.split("@", 1)[0].upper()
                    last_id, _ = watermarks.get(stream, (None, None))
                    if last_id is None:
                        continue
                    rows = await self.rest_client.get_agg_trades(
                        symbol, from_id=last_id + 1, limit=1000
                    )
                    pending_id = None
                    if (
                        pending_message is not None
                        and pending_message.get("e") == "aggTrade"
                        and str(pending_message.get("s", "")).upper() == symbol
                    ):
                        pending_id = int(pending_message["a"])
                        rows = [row for row in rows if int(row["a"]) < pending_id]
                    if pending_id is None and len(rows) >= 1000:
                        raise RecoveryError("aggTrade backfill exceeds one REST page")
                    recovered = self.recovery.aggtrades(
                        rows,
                        expected_start_id=last_id + 1,
                        expected_end_id=(pending_id - 1 if pending_id is not None else None),
                    )
                    agg_batches.append((symbol, recovered))
                elif "@kline_" in stream:
                    symbol, interval = stream.split("@kline_", 1)
                    _, last_close = watermarks.get(stream, (None, None))
                    if last_close is None:
                        continue
                    expected_open = last_close + 1 if last_close is not None else None
                    rows = await self.rest_client.get_klines(
                        symbol.upper(), interval, limit=1500,
                        start_time=expected_open, end_time=now_ms,
                    )
                    if expected_open is not None:
                        rows = [row for row in rows if int(row[0]) >= expected_open]
                    recovered_klines = self.recovery.klines(
                        rows, symbol.upper(), interval, now_ms=now_ms
                    )
                    if (
                        expected_open is not None
                        and recovered_klines
                        and recovered_klines[0].open_time != expected_open
                    ):
                        raise RecoveryError("kline backfill starts with a gap")
                    interval_ms = self.recovery.interval_ms(interval)
                    required_close = (now_ms // interval_ms) * interval_ms - 1
                    recovered_close = (
                        recovered_klines[-1].close_time
                        if recovered_klines else last_close
                    )
                    if recovered_close is not None and recovered_close < required_close:
                        raise RecoveryError("kline backfill exceeds one REST page")
                    kline_batches.append((symbol.upper(), interval, recovered_klines))

            for symbol, batch in agg_batches:
                for row in batch:
                    await self._handle_aggtrade(symbol, row)
            for symbol, interval, batch in kline_batches:
                for kline in batch:
                    await self._handle_kline(symbol, interval, kline)
        except Exception as exc:
            logger.error("行情断流回补失败，保持 fail-closed: %s", type(exc).__name__)
            self.quality.mark_backfill_failed(streams)
            self._quality_generation = generation
            return
        self.quality.begin_connection(streams, generation)
        recovered = self.quality.watermarks()
        for stream, (last_id, last_close) in recovered.items():
            watermark = last_id if "@aggTrade" in stream else last_close
            if watermark is not None:
                self._replay_watermarks[stream] = watermark
        self._quality_generation = generation


    async def _handle_aggtrade(self, symbol: str, trade_data: dict[str, Any]) -> None:
        """处理 aggTrade，聚合为 1s Bar"""
        stream = f"{symbol.lower()}@aggTrade"
        previous = self.quality.watermarks().get(stream, (None, None))
        if not self.quality.observe_aggtrade(
            symbol=symbol,
            aggregate_trade_id=trade_data.get("agg_trade_id"),
            event_time_ms=trade_data["timestamp"],
            received_at_ms=int(time.time() * 1000),
        ):
            return
        try:
            bars = self.aggregator.add_trade(
                symbol=symbol,
                price=trade_data["price"],
                quantity=trade_data["quantity"],
                timestamp=trade_data["timestamp"],
                aggregate_trade_id=trade_data.get("agg_trade_id"),
            )

            for bar in bars:
                await self.redis_publisher.publish_bar1s(bar)
            finalized_trade_id = self.aggregator.last_finalized_trade_id(symbol)
            if finalized_trade_id is not None:
                await self._persist_watermark(stream, finalized_trade_id)
        except Exception:
            self.quality.restore_watermarks(stream, previous)
            raise

    async def _handle_kline(self, symbol: str, interval: str, kline: Any) -> None:
        """处理已完成的 Kline，存储到 Redis Hash"""
        stream = f"{symbol.lower()}@kline_{interval}"
        previous = self.quality.watermarks().get(stream, (None, None))
        if not self.quality.observe_kline(
            symbol=symbol,
            interval=interval,
            open_time_ms=kline.open_time,
            close_time_ms=kline.close_time,
            received_at_ms=int(time.time() * 1000),
        ):
            return
        try:
            await self.kline_store.store_kline(kline)
            await self._persist_watermark(stream, int(kline.close_time))
        except Exception:
            self.quality.restore_watermarks(stream, previous)
            raise

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
        try:
            yield
        finally:
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

    @app.get("/klines/{symbol}/{interval}", response_model=KlineRangeResponse)
    async def historical_klines(
        symbol: str,
        interval: str,
        start_time: int | None = Query(default=None, ge=0),
        end_time: int | None = Query(default=None, ge=0),
        limit: int = Query(default=500, ge=1, le=1500),
    ) -> KlineRangeResponse:
        """HTTP 历史 K 线双栈：只返回已完成、连续且去重的数据。"""
        if start_time is not None and end_time is not None and start_time > end_time:
            raise HTTPException(status_code=400, detail="start_time must not be after end_time")
        try:
            rows = await service.rest_client.get_klines(
                symbol.upper(), interval, limit=limit,
                start_time=start_time, end_time=end_time,
            )
            klines = service.recovery.klines(
                rows, symbol.upper(), interval, now_ms=int(time.time() * 1000)
            )
        except Exception as exc:
            logger.warning("历史 K 线查询失败: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail="historical market data unavailable") from exc
        return KlineRangeResponse(
            symbol=symbol.upper(), interval=interval,
            klines=[kline.to_dict() for kline in klines],
        )

    @app.get("/bar1s/{symbol}/recover", response_model=Bar1sRecoveryResponse)
    async def recover_bar1s(
        symbol: str,
        from_id: int = Query(..., ge=0),
        to_id: int = Query(..., ge=0),
    ) -> Bar1sRecoveryResponse:
        """按 aggTrade ID 回补短缺口；不承担历史行情下载。"""
        if from_id > to_id:
            raise HTTPException(status_code=400, detail="from_id must not exceed to_id")
        count = to_id - from_id + 1
        if count > service.recovery.aggtrade_limit:
            raise HTTPException(status_code=400, detail="recovery range exceeds 1000 trades")
        try:
            rows = await service.rest_client.get_agg_trades(
                symbol.upper(), from_id=from_id, limit=count
            )
            trades = service.recovery.aggtrades(
                rows,
                expected_start_id=from_id,
                expected_end_id=to_id,
            )
            aggregator = Bar1sAggregator()
            bars = []
            for trade in trades:
                bars.extend(
                    aggregator.add_trade(
                        symbol.upper(),
                        trade["price"],
                        trade["quantity"],
                        trade["timestamp"],
                        trade["agg_trade_id"],
                    )
                )
            bars.extend(aggregator.flush_symbol(symbol.upper()))
        except (RecoveryError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            logger.warning("1s Bar 缺口回补失败: %s", type(exc).__name__)
            raise HTTPException(
                status_code=503, detail="market data recovery unavailable"
            ) from exc
        return Bar1sRecoveryResponse(
            symbol=symbol.upper(),
            from_id=from_id,
            to_id=to_id,
            bars=[bar.to_dict() for bar in bars],
        )

    # 更新健康检查，返回运行时长
    @app.get("/health")
    async def health_check(response: Response) -> HealthResponse:
        """健康检查"""
        stats = service.subscription_manager.get_stats()
        try:
            redis_connected = bool(await service.redis.ping())
        except Exception:
            redis_connected = False

        await service._recover_if_generation_changed()
        websocket_required = bool(
            service._current_streams
            or service.subscription_manager.get_active_streams()
        )
        websocket_connected = (
            not websocket_required
            or (
                service._ws_task is not None
                and not service._ws_task.done()
                and service.ws_client.connected
            )
        )
        data_quality_ready = service.quality.ready
        pubsub_delivery_ready = service.redis_publisher.delivery_ready
        pubsub_required = websocket_required
        ready = (
            redis_connected
            and websocket_connected
            and data_quality_ready
            and (not pubsub_required or pubsub_delivery_ready)
        )
        if not ready:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

        return HealthResponse(
            status="ready" if ready else "degraded",
            instance_epoch=stats["instance_epoch"],
            uptime_seconds=service.get_uptime(),
            binance_testnet=service.binance_config.testnet,
            subscribed_symbols=stats["subscribed_symbols"],
            active_ws_streams=stats["active_streams"],
            redis_connected=redis_connected,
            websocket_connected=websocket_connected,
            connection_generation=service.ws_client.connection_generation,
            data_quality_ready=data_quality_ready,
            data_quality_issues=service.quality.issue_count,
            pubsub_delivery_ready=pubsub_delivery_ready,
            pubsub_delivery_issues=service.redis_publisher.delivery_issue_count,
        )

    @app.get("/quality")
    async def quality_status(response: Response) -> QualityResponse:
        await service._recover_if_generation_changed()
        required = bool(
            service._current_streams
            or service.subscription_manager.get_active_streams()
        )
        websocket_connected = service.ws_client.connected
        pubsub_delivery_ready = service.redis_publisher.delivery_ready
        ready = not required or (
            websocket_connected
            and service.quality.ready
            and pubsub_delivery_ready
        )
        if not ready:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return QualityResponse(
            ready=ready,
            websocket_connected=websocket_connected,
            connection_generation=service.ws_client.connection_generation,
            last_connected_at_ms=service.ws_client.last_connected_at_ms,
            last_disconnected_at_ms=service.ws_client.last_disconnected_at_ms,
            streams=service.quality.snapshot(),
            pubsub_delivery_ready=pubsub_delivery_ready,
            pubsub_delivery_issues=service.redis_publisher.delivery_issue_count,
            pubsub_channels=service.redis_publisher.delivery_snapshot(),
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
