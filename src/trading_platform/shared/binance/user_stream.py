"""
Binance User Data Stream 管理
负责 listenKey 管理、WebSocket 连接、订单/账户事件投递、断线重连
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
    - ORDER_TRADE_UPDATE 与 ACCOUNT_UPDATE 事件投递
    """

    def __init__(
        self,
        rest_client: BinanceRestClient,
        ws_base_url: str = "wss://fstream.binance.com",
        on_execution_report: Callable[[dict[str, Any]], None] | None = None,
        on_account_update: Callable[[dict[str, Any]], None] | None = None,
        on_reconnect: Callable[[], None] | None = None,
        on_disconnect: Callable[[], None] | None = None,
        connect_timeout_seconds: float = 10.0,
    ):
        """
        Args:
            rest_client: REST 客户端（用于 listenKey 管理）
            ws_base_url: WebSocket 基础 URL
            on_execution_report: executionReport 事件回调
            on_account_update: ACCOUNT_UPDATE 完整事件回调
            on_reconnect: 重连完成回调
            on_disconnect: 连接断开回调
        """
        if connect_timeout_seconds <= 0:
            raise ValueError("connect_timeout_seconds must be positive")
        self.rest_client = rest_client
        self.ws_base_url = ws_base_url.rstrip('/')
        self.on_execution_report = on_execution_report
        self.on_account_update = on_account_update
        self.on_reconnect = on_reconnect
        self.on_disconnect = on_disconnect
        self.connect_timeout_seconds = connect_timeout_seconds

        self.listen_key: str | None = None
        self.ws: websocket.WebSocketApp | None = None
        self._keepalive_task: asyncio.Task | None = None
        self._ws_thread: asyncio.Task | None = None
        self._reconnect_task: Future | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._connected_event: asyncio.Event | None = None
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
        self._connected_event = asyncio.Event()

        try:
            # 创建 listenKey
            self.listen_key = await self.rest_client.create_listen_key()
            logger.info(f"Created listenKey: {self.listen_key[:10]}...")

            # 启动 keepalive 任务
            self._keepalive_task = asyncio.create_task(self._keepalive_loop())

            # 启动 WebSocket 连接
            await self._connect_ws()
            await self._wait_until_connected()
        except Exception:
            await self.stop()
            raise

    async def stop(self) -> None:
        """停止 User Data Stream"""
        if not self._running and not any(
            (self._keepalive_task, self._ws_thread, self._reconnect_task, self.listen_key)
        ):
            return

        self._running = False
        self._mark_disconnected()

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

        if self._ws_thread and not self._ws_thread.done():
            self._ws_thread.cancel()
            try:
                await self._ws_thread
            except asyncio.CancelledError:
                pass
        self._ws_thread = None

        # 取消 keepalive 任务
        if self._keepalive_task:
            self._keepalive_task.cancel()
            try:
                await self._keepalive_task
            except asyncio.CancelledError:
                pass
            self._keepalive_task = None

        # 关闭 listenKey
        if self.listen_key:
            try:
                await self.rest_client.close_listen_key(self.listen_key)
                logger.info("Closed listenKey")
            except Exception as e:
                logger.error(f"Failed to close listenKey: {e}")

        self.listen_key = None
        self._loop = None

    @property
    def connected(self) -> bool:
        event = self._connected_event
        return event is not None and event.is_set()

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
        if self._connected_event is None:
            self._connected_event = asyncio.Event()
        self._mark_disconnected()

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
                    if self.on_account_update:
                        self._schedule(self._handle_account_update(data))
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
            loop = self._loop
            if loop and not loop.is_closed():
                loop.call_soon_threadsafe(self._mark_disconnected)
            if self._running:
                self._schedule_reconnect()

        def on_open(ws):
            """WebSocket 打开回调"""
            logger.info("User Data Stream connected")
            self._reconnect_delay = 1.0  # 重置重连延迟
            loop = self._loop
            if loop and not loop.is_closed():
                loop.call_soon_threadsafe(self._mark_connected)

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

    async def _wait_until_connected(self) -> None:
        event = self._connected_event
        ws_task = self._ws_thread
        if event is None or ws_task is None:
            raise RuntimeError("User Data Stream connection was not initialized")
        connected = asyncio.create_task(event.wait())
        try:
            done, _ = await asyncio.wait(
                {connected, ws_task},
                timeout=self.connect_timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if connected in done and connected.result():
                return
            if ws_task in done:
                await ws_task
                raise RuntimeError("User Data Stream closed before opening")
            raise TimeoutError("User Data Stream connection timed out")
        finally:
            if not connected.done():
                connected.cancel()
                await asyncio.gather(connected, return_exceptions=True)

    async def _reconnect(self) -> None:
        """重连逻辑"""
        while self._running:
            logger.info(f"Reconnecting in {self._reconnect_delay} seconds...")
            await asyncio.sleep(self._reconnect_delay)
            self._reconnect_delay = min(
                self._reconnect_delay * 2, self._max_reconnect_delay
            )
            try:
                old_listen_key = self.listen_key
                if old_listen_key:
                    try:
                        await self.rest_client.close_listen_key(old_listen_key)
                    except Exception:
                        logger.warning("Failed to close stale listenKey", exc_info=True)
                self.listen_key = await self.rest_client.create_listen_key()
                logger.info(f"Recreated listenKey: {self.listen_key[:10]}...")
                await self._connect_ws()
                await self._wait_until_connected()
                if self.on_reconnect:
                    result = self.on_reconnect()
                    if inspect.isawaitable(result):
                        await result
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Reconnect failed: {e}", exc_info=True)

    async def _reconnect_after_disconnect(self) -> None:
        if self.on_disconnect:
            try:
                result = self.on_disconnect()
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.error("User Data Stream disconnect callback failed", exc_info=True)
        await self._reconnect()

    def _mark_connected(self) -> None:
        if self._connected_event is not None:
            self._connected_event.set()

    def _mark_disconnected(self) -> None:
        if self._connected_event is not None:
            self._connected_event.clear()

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
            self._reconnect_task = asyncio.run_coroutine_threadsafe(
                self._reconnect_after_disconnect(), loop
            )
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

    async def _handle_account_update(self, event: dict[str, Any]) -> None:
        """处理完整 ACCOUNT_UPDATE 事件。"""
        if not self.on_account_update:
            return
        try:
            result = self.on_account_update(event)
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.error("Error in account update callback", exc_info=True)


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
