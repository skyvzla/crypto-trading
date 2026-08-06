"""
Binance User Data Stream 管理
负责 listenKey 管理、WebSocket 连接、executionReport 解析、断线重连
"""
import asyncio
import inspect
import json
import logging
from concurrent.futures import Future
from typing import Callable, Any

import websocket

from .rest_client import BinanceRestClient

logger = logging.getLogger(__name__)


class UserDataStream:
    """
    User Data Stream 管理器

    职责：
    - listenKey 创建和 keepalive（30分钟一次）
    - WebSocket 连接和自动重连
    - executionReport 事件解析和回调
    """

    def __init__(
        self,
        rest_client: BinanceRestClient,
        ws_base_url: str = "wss://fstream.binance.com",
        on_execution_report: Callable[[dict[str, Any]], None] | None = None,
        on_reconnect: Callable[[], None] | None = None,
    ):
        """
        Args:
            rest_client: REST 客户端（用于 listenKey 管理）
            ws_base_url: WebSocket 基础 URL
            on_execution_report: executionReport 事件回调
            on_reconnect: 重连完成回调
        """
        self.rest_client = rest_client
        self.ws_base_url = ws_base_url.rstrip('/')
        self.on_execution_report = on_execution_report
        self.on_reconnect = on_reconnect

        self.listen_key: str | None = None
        self.ws: websocket.WebSocketApp | None = None
        self._keepalive_task: asyncio.Task | None = None
        self._ws_thread: asyncio.Task | None = None
        self._reconnect_task: Future | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._running = False
        self._reconnect_delay = 1.0  # 初始重连延迟（秒）
        self._max_reconnect_delay = 60.0  # 最大重连延迟

    async def start(self) -> None:
        """启动 User Data Stream"""
        if self._running:
            logger.warning("User Data Stream already running")
            return

        self._running = True
        self._loop = asyncio.get_running_loop()

        # 创建 listenKey
        self.listen_key = await self.rest_client.create_listen_key()
        logger.info(f"Created listenKey: {self.listen_key[:10]}...")

        # 启动 keepalive 任务
        self._keepalive_task = asyncio.create_task(self._keepalive_loop())

        # 启动 WebSocket 连接
        await self._connect_ws()

    async def stop(self) -> None:
        """停止 User Data Stream"""
        if not self._running:
            return

        self._running = False

        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()
            try:
                await asyncio.wrap_future(self._reconnect_task)
            except asyncio.CancelledError:
                pass
        self._reconnect_task = None

        # 关闭 WebSocket
        if self.ws:
            self.ws.close()
            self.ws = None

        # 取消 keepalive 任务
        if self._keepalive_task:
            self._keepalive_task.cancel()
            try:
                await self._keepalive_task
            except asyncio.CancelledError:
                pass

        # 关闭 listenKey
        if self.listen_key:
            try:
                await self.rest_client.close_listen_key(self.listen_key)
                logger.info("Closed listenKey")
            except Exception as e:
                logger.error(f"Failed to close listenKey: {e}")

        self.listen_key = None
        self._loop = None

    async def _keepalive_loop(self) -> None:
        """
        listenKey keepalive 循环
        每 30 分钟发送一次 keepalive 请求
        """
        while self._running:
            try:
                await asyncio.sleep(30 * 60)  # 30 分钟
                if self.listen_key and self._running:
                    await self.rest_client.keepalive_listen_key(self.listen_key)
                    logger.debug("listenKey keepalive sent")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"listenKey keepalive failed: {e}")

    async def _connect_ws(self) -> None:
        """建立 WebSocket 连接"""
        if not self.listen_key:
            raise RuntimeError("listenKey not created")

        ws_url = f"{self.ws_base_url}/ws/{self.listen_key}"

        def on_message(ws, message):
            """WebSocket 消息回调"""
            try:
                data = json.loads(message)
                event_type = data.get('e')

                if event_type == 'ORDER_TRADE_UPDATE':
                    # executionReport 事件
                    order_data = data.get('o', {})
                    if self.on_execution_report:
                        self._schedule(self._handle_execution_report(order_data))
                elif event_type == 'ACCOUNT_UPDATE':
                    # 账户更新事件（可选处理）
                    logger.debug(f"Account update: {data}")
                else:
                    logger.debug(f"Unknown event type: {event_type}")

            except Exception as e:
                logger.error(f"Error processing message: {e}", exc_info=True)

        def on_error(ws, error):
            """WebSocket 错误回调"""
            logger.error(f"WebSocket error: {error}")

        def on_close(ws, close_status_code, close_msg):
            """WebSocket 关闭回调"""
            logger.warning(f"WebSocket closed: {close_status_code} {close_msg}")
            if self._running:
                self._schedule_reconnect()

        def on_open(ws):
            """WebSocket 打开回调"""
            logger.info("User Data Stream connected")
            self._reconnect_delay = 1.0  # 重置重连延迟

        self.ws = websocket.WebSocketApp(
            ws_url,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
            on_open=on_open,
        )

        # 在独立线程中运行 WebSocket
        self._ws_thread = asyncio.create_task(self._run_ws())

    async def _run_ws(self) -> None:
        """在事件循环中运行 WebSocket"""
        if not self.ws:
            return

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self.ws.run_forever)

    async def _reconnect(self) -> None:
        """重连逻辑"""
        if not self._running:
            return

        logger.info(f"Reconnecting in {self._reconnect_delay} seconds...")
        await asyncio.sleep(self._reconnect_delay)

        # 指数退避
        self._reconnect_delay = min(self._reconnect_delay * 2, self._max_reconnect_delay)

        try:
            # 重新创建 listenKey
            self.listen_key = await self.rest_client.create_listen_key()
            logger.info(f"Recreated listenKey: {self.listen_key[:10]}...")

            # 重新连接 WebSocket
            await self._connect_ws()

            # 触发重连回调
            if self.on_reconnect:
                result = self.on_reconnect()
                if inspect.isawaitable(result):
                    await result

        except Exception as e:
            logger.error(f"Reconnect failed: {e}", exc_info=True)
            # 继续重试
            await self._reconnect()

    def _schedule(self, coro) -> None:
        """将 websocket-client 线程中的协程安全投递到主事件循环。"""
        loop = self._loop
        if not loop or loop.is_closed():
            coro.close()
            return
        try:
            future = asyncio.run_coroutine_threadsafe(coro, loop)
            future.add_done_callback(self._log_scheduled_error)
        except RuntimeError:
            coro.close()

    @staticmethod
    def _log_scheduled_error(future: Future) -> None:
        if future.cancelled():
            return
        try:
            future.result()
        except Exception:
            logger.error("Scheduled User Data Stream task failed", exc_info=True)

    def _schedule_reconnect(self) -> None:
        if not self._running or self._reconnect_task and not self._reconnect_task.done():
            return
        loop = self._loop
        if not loop or loop.is_closed():
            return
        try:
            self._reconnect_task = asyncio.run_coroutine_threadsafe(self._reconnect(), loop)
            self._reconnect_task.add_done_callback(self._log_scheduled_error)
        except RuntimeError:
            self._reconnect_task = None

    async def _handle_execution_report(self, order_data: dict[str, Any]) -> None:
        """
        处理 executionReport 事件

        Args:
            order_data: 订单数据（executionReport 的 'o' 字段）
        """
        if not self.on_execution_report:
            return

        try:
            # 异步调用回调
            result = self.on_execution_report(order_data)
            if inspect.isawaitable(result):
                await result
        except Exception as e:
            logger.error(f"Error in execution report callback: {e}", exc_info=True)


async def main_example():
    """使用示例"""
    from .rest_client import BinanceRestClient

    async def on_execution_report(order_data):
        client_order_id = order_data['c']
        status = order_data['X']
        print(f"Order update: {client_order_id} -> {status}")

    async def on_reconnect():
        print("Reconnected, running reconciliation...")
        # 这里调用启动对账逻辑

    rest_client = BinanceRestClient(
        api_key="YOUR_API_KEY",
        api_secret="YOUR_API_SECRET",
    )

    stream = UserDataStream(
        rest_client=rest_client,
        on_execution_report=on_execution_report,
        on_reconnect=on_reconnect,
    )

    try:
        await stream.start()
        await asyncio.sleep(3600)  # 运行1小时
    finally:
        await stream.stop()
        await rest_client.close()


if __name__ == "__main__":
    asyncio.run(main_example())
