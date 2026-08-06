"""
Binance WebSocket 接入
订阅 aggTrade 流和 Kline 流，提供连接管理和错误重连
"""
import asyncio
import json
import logging
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection
from websockets.exceptions import ConnectionClosed, WebSocketException

from trading_platform.shared.events import Bar1s, Kline


logger = logging.getLogger(__name__)


def unwrap_stream_message(data: dict[str, Any]) -> dict[str, Any]:
    """Return the event payload from either a raw or combined stream message."""
    payload = data.get("data")
    return payload if isinstance(payload, dict) else data


class BinanceWebSocketClient:
    """
    Binance WebSocket 客户端
    支持 aggTrade 和 Kline 流订阅，自动重连
    """

    def __init__(
        self,
        ws_base_url: str = "wss://fstream.binance.com",
        reconnect_delay: float = 5.0,
        max_reconnect_attempts: int = 0,  # 0 = 无限重试
    ):
        self.ws_base_url = ws_base_url
        self.reconnect_delay = reconnect_delay
        self.max_reconnect_attempts = max_reconnect_attempts
        self._ws: ClientConnection | None = None
        self._running = False
        self._reconnect_count = 0
        self._streams: tuple[str, ...] = ()

    def _stream_url(self) -> str:
        stream_path = "/".join(self._streams)
        return f"{self.ws_base_url}/stream?streams={stream_path}"

    async def _open_connection(self) -> None:
        url = self._stream_url()
        self._ws = await websockets.connect(
            url,
            ping_interval=20,
            ping_timeout=10,
            close_timeout=5,
        )

    async def connect(self, streams: list[str]) -> None:
        """
        连接到 Binance WebSocket 组合流

        Args:
            streams: 流名称列表，例如 ["btcusdt@aggTrade", "ethusdt@kline_1m"]
        """
        if not streams:
            raise ValueError("至少需要一个流")

        self._streams = tuple(sorted(set(streams)))
        url = self._stream_url()

        logger.info(f"连接 Binance WebSocket: {url}")

        try:
            await self._open_connection()
            self._running = True
            self._reconnect_count = 0
            logger.info(f"WebSocket 连接成功，订阅 {len(streams)} 个流")
        except Exception as e:
            logger.error(f"WebSocket 连接失败: {e}")
            raise

    async def disconnect(self) -> None:
        """断开连接"""
        self._running = False
        if self._ws:
            await self._ws.close()
            self._ws = None
        self._streams = ()
        logger.info("WebSocket 已断开")

    async def _reconnect(self) -> bool:
        """Reconnect using the last successful subscription set."""
        while self._running:
            if (
                self.max_reconnect_attempts > 0
                and self._reconnect_count >= self.max_reconnect_attempts
            ):
                logger.error(f"达到最大重连次数 {self.max_reconnect_attempts}，停止重连")
                self._running = False
                return False

            self._reconnect_count += 1
            logger.info(
                f"等待 {self.reconnect_delay}s 后重连（第 {self._reconnect_count} 次）"
            )
            await asyncio.sleep(self.reconnect_delay)
            if not self._running:
                return False

            try:
                await self._open_connection()
                if not self._running:
                    if self._ws is not None:
                        await self._ws.close()
                        self._ws = None
                    return False
                logger.info(f"WebSocket 重连成功，订阅 {len(self._streams)} 个流")
                return True
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._ws = None
                logger.warning(f"WebSocket 重连失败: {exc}")

        return False

    async def receive_messages(self) -> AsyncIterator[dict[str, Any]]:
        """
        接收消息流，自动处理重连

        Yields:
            解析后的 JSON 消息字典
        """
        while self._running:
            try:
                if not self._ws:
                    raise ConnectionClosed(None, None)

                message = await self._ws.recv()

                if isinstance(message, bytes):
                    message = message.decode("utf-8")

                try:
                    data = json.loads(message)
                    # Binance combined streams wrap the event in {stream, data}.
                    # Keep the downstream parsers independent of transport mode.
                    if isinstance(data, dict):
                        self._reconnect_count = 0
                        yield unwrap_stream_message(data)
                except json.JSONDecodeError as e:
                    logger.warning(f"无法解析 WebSocket 消息: {e}, message: {message[:100]}")
                    continue

            except (ConnectionClosed, WebSocketException) as e:
                logger.warning(f"WebSocket 连接断开: {e}")

                if not self._running:
                    break

                self._ws = None
                if not await self._reconnect():
                    break

            except Exception as e:
                logger.error(f"接收消息时发生未预期错误: {e}", exc_info=True)
                raise


def parse_aggtrade_message(data: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """
    解析 aggTrade 消息

    Returns:
        (symbol, trade_data) 或 None（如果不是 aggTrade 消息）
    """
    if data.get("e") != "aggTrade":
        return None

    return data["s"], {
        "agg_trade_id": data["a"],
        "price": Decimal(data["p"]),
        "quantity": Decimal(data["q"]),
        "first_trade_id": data["f"],
        "last_trade_id": data["l"],
        "timestamp": data["T"],
        "is_buyer_maker": data["m"],
    }


def parse_kline_message(data: dict[str, Any]) -> tuple[str, str, Kline] | None:
    """
    解析 Kline 消息，只处理 isFinal=true

    Returns:
        (symbol, interval, Kline) 或 None（如果不是已完成的 K 线）
    """
    if data.get("e") != "kline":
        return None

    k = data["k"]

    # 只处理已完成的 K 线
    if not k["x"]:
        return None

    symbol = data["s"]
    interval = k["i"]
    open_time = k["t"]
    close_time = k["T"]

    kline = Kline(
        symbol=symbol,
        interval=interval,
        open_time=open_time,
        close_time=close_time,
        available_time=close_time + 1,
        open=Decimal(k["o"]),
        high=Decimal(k["h"]),
        low=Decimal(k["l"]),
        close=Decimal(k["c"]),
        volume=Decimal(k["v"]),
        type_priority=2,
        sequence=0,
    )

    return symbol, interval, kline


def build_stream_names(symbols: list[str], subscription_types: list[str]) -> list[str]:
    """
    构建 Binance WebSocket 流名称列表

    Args:
        symbols: 交易对列表，例如 ["BTCUSDT", "ETHUSDT"]
        subscription_types: 订阅类型列表，例如 ["bar1s", "kline:1m"]

    Returns:
        流名称列表，例如 ["btcusdt@aggTrade", "btcusdt@kline_1m"]
    """
    streams = []

    for symbol in symbols:
        symbol_lower = symbol.lower()

        for sub_type in subscription_types:
            if sub_type == "bar1s":
                # bar1s 需要订阅 aggTrade
                streams.append(f"{symbol_lower}@aggTrade")
            elif sub_type.startswith("kline:"):
                # kline:1m -> kline_1m
                interval = sub_type.split(":", 1)[1]
                streams.append(f"{symbol_lower}@kline_{interval}")

    # 去重
    return list(set(streams))
