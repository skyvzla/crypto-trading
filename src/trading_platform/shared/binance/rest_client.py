"""
Binance Futures REST API 客户端
使用 httpx 异步调用，支持签名、限速、重试
"""
import hashlib
import hmac
import time
from decimal import Decimal
from typing import Any, Literal
from urllib.parse import urlencode

import httpx

from .rate_limiter import DEFAULT_RATE_LIMITER, RateLimiter, get_endpoint_weight


class BinanceAPIException(Exception):
    """Binance API 异常"""
    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message
        super().__init__(f"Binance API Error {code}: {message}")


class BinanceRestClient:
    """
    Binance Futures REST 客户端

    职责：
    - 下单 (POST /fapi/v1/order)
    - 撤单 (DELETE /fapi/v1/order)
    - 查单 (GET /fapi/v1/order)
    - 查询持仓 (GET /fapi/v2/positionRisk)
    - 查询账户 (GET /fapi/v2/account)
    - 签名和限速
    """

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        base_url: str = "https://fapi.binance.com",
        rate_limiter: RateLimiter | None = None,
        timeout: float = 10.0,
    ):
        """
        Args:
            api_key: Binance API Key
            api_secret: Binance API Secret
            base_url: API 基础 URL
            rate_limiter: 限速器（默认使用全局实例）
            timeout: 请求超时时间（秒）
        """
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url.rstrip('/')
        self.rate_limiter = rate_limiter or DEFAULT_RATE_LIMITER
        self.timeout = timeout

        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self.timeout,
            headers={
                "X-MBX-APIKEY": self.api_key,
                "Content-Type": "application/x-www-form-urlencoded",
            }
        )

    async def close(self) -> None:
        """关闭 HTTP 客户端"""
        await self._client.aclose()

    def _sign(self, params: dict[str, Any]) -> dict[str, Any]:
        """
        为请求参数添加签名

        Args:
            params: 请求参数

        Returns:
            添加了签名的参数字典
        """
        # 添加时间戳
        params['timestamp'] = int(time.time() * 1000)

        # 生成查询字符串
        query_string = urlencode(params)

        # 计算签名
        signature = hmac.new(
            self.api_secret.encode('utf-8'),
            query_string.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()

        params['signature'] = signature
        return params

    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        signed: bool = True,
    ) -> dict[str, Any]:
        """
        发送 HTTP 请求

        Args:
            method: HTTP 方法
            path: 请求路径
            params: 请求参数
            signed: 是否需要签名

        Returns:
            响应 JSON

        Raises:
            BinanceAPIException: API 错误
            httpx.TimeoutException: 请求超时
        """
        params = params or {}

        # 签名
        if signed:
            params = self._sign(params)

        # 限速
        weight = get_endpoint_weight(method, path)
        await self.rate_limiter.acquire(weight)

        # 发送请求
        try:
            if method == 'GET':
                response = await self._client.get(path, params=params)
            elif method == 'POST':
                response = await self._client.post(path, data=params)
            elif method == 'DELETE':
                response = await self._client.delete(path, params=params)
            elif method == 'PUT':
                response = await self._client.put(path, data=params)
            else:
                raise ValueError(f"Unsupported method: {method}")

            # 检查响应
            if response.status_code == 200:
                return response.json()
            else:
                error_data = response.json()
                raise BinanceAPIException(
                    code=error_data.get('code', -1),
                    message=error_data.get('msg', 'Unknown error')
                )

        except httpx.TimeoutException:
            raise
        except BinanceAPIException:
            raise
        except Exception as e:
            raise RuntimeError(f"Request failed: {e}") from e

    # ========== 订单接口 ==========

    async def get_exchange_info(self) -> dict[str, Any]:
        """读取 USD-M Futures 交易规则；该接口无需签名。"""
        return await self._request('GET', '/fapi/v1/exchangeInfo', {}, signed=False)

    async def post_order(
        self,
        symbol: str,
        side: Literal['BUY', 'SELL'],
        order_type: Literal['LIMIT', 'MARKET', 'STOP', 'TAKE_PROFIT'],
        quantity: Decimal,
        price: Decimal | None = None,
        time_in_force: Literal['GTC', 'IOC', 'FOK', 'GTX'] = 'GTC',
        new_client_order_id: str | None = None,
        reduce_only: bool = False,
    ) -> dict[str, Any]:
        """
        下单

        Args:
            symbol: 交易对
            side: 买卖方向
            order_type: 订单类型
            quantity: 数量
            price: 价格（限价单必填）
            time_in_force: 有效方式
            new_client_order_id: 自定义订单ID
            reduce_only: 只减仓

        Returns:
            订单响应
        """
        params: dict[str, Any] = {
            'symbol': symbol,
            'side': side,
            'type': order_type,
            'quantity': str(quantity),
        }

        if price is not None:
            params['price'] = str(price)

        if order_type == 'LIMIT':
            params['timeInForce'] = time_in_force

        if new_client_order_id:
            params['newClientOrderId'] = new_client_order_id

        if reduce_only:
            params['reduceOnly'] = 'true'

        return await self._request('POST', '/fapi/v1/order', params)

    async def cancel_order(
        self,
        symbol: str,
        order_id: int | None = None,
        orig_client_order_id: str | None = None,
    ) -> dict[str, Any]:
        """
        撤单

        Args:
            symbol: 交易对
            order_id: 交易所订单ID
            orig_client_order_id: 自定义订单ID

        Returns:
            撤单响应

        Raises:
            ValueError: order_id 和 orig_client_order_id 必须提供一个
        """
        if not order_id and not orig_client_order_id:
            raise ValueError("Must provide either order_id or orig_client_order_id")

        params: dict[str, Any] = {'symbol': symbol}

        if order_id:
            params['orderId'] = order_id
        if orig_client_order_id:
            params['origClientOrderId'] = orig_client_order_id

        return await self._request('DELETE', '/fapi/v1/order', params)

    async def query_order(
        self,
        symbol: str,
        order_id: int | None = None,
        orig_client_order_id: str | None = None,
    ) -> dict[str, Any] | None:
        """
        查询订单

        Args:
            symbol: 交易对
            order_id: 交易所订单ID
            orig_client_order_id: 自定义订单ID

        Returns:
            订单信息，如果不存在返回 None

        Raises:
            ValueError: order_id 和 orig_client_order_id 必须提供一个
        """
        if not order_id and not orig_client_order_id:
            raise ValueError("Must provide either order_id or orig_client_order_id")

        params: dict[str, Any] = {'symbol': symbol}

        if order_id:
            params['orderId'] = order_id
        if orig_client_order_id:
            params['origClientOrderId'] = orig_client_order_id

        try:
            return await self._request('GET', '/fapi/v1/order', params)
        except BinanceAPIException as e:
            if e.code == -2013:  # Order does not exist
                return None
            raise

    async def get_open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        """
        查询当前活跃订单

        Args:
            symbol: 交易对（可选，不传则查询所有）

        Returns:
            订单列表
        """
        params = {}
        if symbol:
            params['symbol'] = symbol

        return await self._request('GET', '/fapi/v1/openOrders', params)

    # ========== 账户接口 ==========

    async def get_account(self) -> dict[str, Any]:
        """
        查询账户信息

        Returns:
            账户信息（余额、保证金等）
        """
        return await self._request('GET', '/fapi/v2/account', {})

    async def get_position_mode(self) -> dict[str, Any]:
        """查询账户是单向持仓还是双向持仓模式。"""
        return await self._request('GET', '/fapi/v1/positionSide/dual', {})

    async def get_position_risk(self, symbol: str | None = None) -> list[dict[str, Any]]:
        """
        查询持仓风险

        Args:
            symbol: 交易对（可选）

        Returns:
            持仓列表
        """
        params = {}
        if symbol:
            params['symbol'] = symbol

        return await self._request('GET', '/fapi/v2/positionRisk', params)

    async def get_account_trades(
        self,
        symbol: str,
        *,
        start_time: int | None = None,
        end_time: int | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """查询账户成交历史，供启动时恢复错过的 User Stream 成交。"""
        if not symbol:
            raise ValueError("symbol is required")
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        params: dict[str, Any] = {"symbol": symbol, "limit": limit}
        if start_time is not None:
            params["startTime"] = start_time
        if end_time is not None:
            params["endTime"] = end_time
        result = await self._request('GET', '/fapi/v1/userTrades', params)
        if not isinstance(result, list):
            raise RuntimeError("invalid Binance account trades response")
        return result

    async def get_klines(
        self,
        symbol: str,
        interval: str,
        *,
        limit: int = 500,
        end_time: int | None = None,
    ) -> list[list[Any]]:
        """读取公开的已完成 K 线候选数据，供实时策略启动预热。"""
        if not 1 <= limit <= 1500:
            raise ValueError("limit must be between 1 and 1500")
        params: dict[str, Any] = {
            'symbol': symbol,
            'interval': interval,
            'limit': limit,
        }
        if end_time is not None:
            params['endTime'] = end_time
        result = await self._request(
            'GET', '/fapi/v1/klines', params, signed=False
        )
        if not isinstance(result, list):
            raise RuntimeError("invalid Binance kline response")
        return result

    # ========== User Data Stream 接口 ==========

    async def create_listen_key(self) -> str:
        """
        创建 listenKey

        Returns:
            listenKey 字符串
        """
        result = await self._request('POST', '/fapi/v1/listenKey', {}, signed=False)
        return result['listenKey']

    async def keepalive_listen_key(self, listen_key: str) -> None:
        """
        延长 listenKey 有效期

        Args:
            listen_key: listenKey
        """
        await self._request('PUT', '/fapi/v1/listenKey', {'listenKey': listen_key}, signed=False)

    async def close_listen_key(self, listen_key: str) -> None:
        """
        关闭 listenKey

        Args:
            listen_key: listenKey
        """
        await self._request('DELETE', '/fapi/v1/listenKey', {'listenKey': listen_key}, signed=False)
