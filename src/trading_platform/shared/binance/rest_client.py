"""
Binance Futures REST API 客户端
使用 httpx 异步调用，支持签名、限速、重试
"""
import asyncio
import hashlib
import hmac
import inspect
import re
import time
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any, Literal
from urllib.parse import urlencode

import httpx

from .rate_limiter import DEFAULT_RATE_LIMITER, RateLimiter, get_endpoint_weight


EventObserver = Callable[[str, dict[str, Any]], Awaitable[None] | None]

_BINANCE_RATE_HEADERS = (
    "X-MBX-USED-WEIGHT-1M",
    "X-MBX-USED-WEIGHT-1S",
    "X-MBX-ORDER-COUNT-10S",
    "X-MBX-ORDER-COUNT-1D",
    "Retry-After",
)
_SENSITIVE_QUERY_VALUE = re.compile(
    r"(?i)(api[_-]?key|secret|signature|authorization|listen[_-]?key|password|token)"
    r"(\s*[=:]\s*)([^&\s,;]+)"
)
_REDACTED = "[REDACTED]"


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
        event_observer: EventObserver | None = None,
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
        self.event_observer = event_observer

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

    @staticmethod
    def _collect_sensitive_values(value: Any) -> set[str]:
        values: set[str] = set()
        if isinstance(value, dict):
            for key, child in value.items():
                normalized = str(key).replace("_", "").replace("-", "").lower()
                if any(
                    marker in normalized
                    for marker in (
                        "apikey",
                        "secret",
                        "signature",
                        "authorization",
                        "listenkey",
                        "password",
                        "token",
                    )
                ):
                    if isinstance(child, (str, int, float)):
                        values.add(str(child))
                values.update(BinanceRestClient._collect_sensitive_values(child))
        elif isinstance(value, (list, tuple)):
            for child in value:
                values.update(BinanceRestClient._collect_sensitive_values(child))
        return values

    def _redact_observer_value(
        self, value: Any, *, sensitive_values: set[str] | None = None
    ) -> Any:
        """Redact credentials both by field name and from textual errors."""

        from trading_platform.shared.execution_event_journal import (
            redact_event_details,
        )

        safe = redact_event_details(value)
        values = set(sensitive_values or ())
        if self.api_key:
            values.add(self.api_key)
        if self.api_secret:
            values.add(self.api_secret)

        def replace_text(text: str) -> str:
            for secret in sorted(values, key=len, reverse=True):
                if secret:
                    text = text.replace(secret, _REDACTED)
            return _SENSITIVE_QUERY_VALUE.sub(
                lambda match: f"{match.group(1)}{match.group(2)}{_REDACTED}",
                text,
            )

        if isinstance(safe, str):
            return replace_text(safe)
        if isinstance(safe, dict):
            return {
                key: self._redact_observer_value(item, sensitive_values=values)
                for key, item in safe.items()
            }
        if isinstance(safe, list):
            return [
                self._redact_observer_value(item, sensitive_values=values)
                for item in safe
            ]
        if isinstance(safe, tuple):
            return tuple(
                self._redact_observer_value(item, sensitive_values=values)
                for item in safe
            )
        return safe

    async def _observe(self, stage: str, details: dict[str, Any]) -> None:
        observer = self.event_observer
        if observer is None:
            return
        result = observer(stage, details)
        if inspect.isawaitable(result):
            await result

    async def _observe_preserving_error(
        self,
        stage: str,
        details: dict[str, Any],
        primary_error: BaseException,
    ) -> None:
        """Best-effort terminal observation that never replaces the request error."""

        try:
            await self._observe(stage, details)
        except BaseException as observer_error:
            primary_error.add_note(
                f"request {stage} observer failed: "
                f"{type(observer_error).__name__}: {observer_error}"
            )

    @staticmethod
    def _response_body(response: httpx.Response) -> Any:
        try:
            return response.json()
        except (ValueError, TypeError):
            return response.text

    @staticmethod
    def _rate_headers(response: httpx.Response) -> dict[str, str]:
        return {
            name: response.headers[name]
            for name in _BINANCE_RATE_HEADERS
            if name in response.headers
        }

    def _observer_details(
        self,
        *,
        method: str,
        path: str,
        params: dict[str, Any],
        started_at: float,
        response: httpx.Response | None = None,
        response_body: Any = None,
        error: BaseException | str | None = None,
    ) -> dict[str, Any]:
        sensitive_values = self._collect_sensitive_values(params)
        details: dict[str, Any] = {
            "method": method,
            "path": self._redact_observer_value(
                path, sensitive_values=sensitive_values
            ),
            "params": self._redact_observer_value(
                params, sensitive_values=sensitive_values
            ),
            "status_code": response.status_code if response is not None else None,
            "rate_headers": (
                self._rate_headers(response) if response is not None else {}
            ),
            "duration_ms": max(0, round((time.monotonic() - started_at) * 1000, 3)),
            "response_body": self._redact_observer_value(
                response_body, sensitive_values=sensitive_values
            ),
        }
        if error is not None:
            details["error"] = self._redact_observer_value(
                str(error), sensitive_values=sensitive_values
            )
            details["error_type"] = type(error).__name__
        return details

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
        params = dict(params or {})

        # 签名
        if signed:
            params = self._sign(params)

        # 限速
        weight = get_endpoint_weight(method, path)
        await self.rate_limiter.acquire(weight)

        started_at = time.monotonic()
        # Observer failure before this point intentionally prevents the network call.
        await self._observe(
            "request_started",
            self._observer_details(
                method=method,
                path=path,
                params=params,
                started_at=started_at,
            ),
        )

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
        except asyncio.CancelledError as error:
            await self._observe_preserving_error(
                "request_cancelled",
                self._observer_details(
                    method=method,
                    path=path,
                    params=params,
                    started_at=started_at,
                    error=error,
                ),
                error,
            )
            raise
        except httpx.TimeoutException as error:
            await self._observe_preserving_error(
                "request_failed",
                self._observer_details(
                    method=method,
                    path=path,
                    params=params,
                    started_at=started_at,
                    error=error,
                ),
                error,
            )
            raise
        except BinanceAPIException as error:
            await self._observe_preserving_error(
                "request_failed",
                self._observer_details(
                    method=method,
                    path=path,
                    params=params,
                    started_at=started_at,
                    error=error,
                ),
                error,
            )
            raise
        except Exception as error:
            request_error = RuntimeError(f"Request failed: {error}")
            await self._observe_preserving_error(
                "request_failed",
                self._observer_details(
                    method=method,
                    path=path,
                    params=params,
                    started_at=started_at,
                    error=error,
                ),
                request_error,
            )
            raise request_error from error

        try:
            response_body = self._response_body(response)
        except asyncio.CancelledError as error:
            await self._observe_preserving_error(
                "request_cancelled",
                self._observer_details(
                    method=method,
                    path=path,
                    params=params,
                    started_at=started_at,
                    error=error,
                ),
                error,
            )
            raise
        except Exception as error:
            request_error = RuntimeError(f"Request failed: {error}")
            await self._observe_preserving_error(
                "request_failed",
                self._observer_details(
                    method=method,
                    path=path,
                    params=params,
                    started_at=started_at,
                    error=error,
                ),
                request_error,
            )
            raise request_error from error
        if 200 <= response.status_code < 300:
            try:
                payload = response.json()
            except asyncio.CancelledError as error:
                await self._observe_preserving_error(
                    "request_cancelled",
                    self._observer_details(
                        method=method,
                        path=path,
                        params=params,
                        started_at=started_at,
                        response=response,
                        response_body=response_body,
                        error=error,
                    ),
                    error,
                )
                raise
            except Exception as error:
                request_error = RuntimeError(f"Request failed: {error}")
                await self._observe_preserving_error(
                    "request_failed",
                    self._observer_details(
                        method=method,
                        path=path,
                        params=params,
                        started_at=started_at,
                        response=response,
                        response_body=response_body,
                        error=error,
                    ),
                    request_error,
                )
                raise request_error from error
            await self._observe(
                "request_succeeded",
                self._observer_details(
                    method=method,
                    path=path,
                    params=params,
                    started_at=started_at,
                    response=response,
                    response_body=payload,
                ),
            )
            return payload

        try:
            error_data = response.json()
            error_code = error_data.get('code', -1)
            error_message = error_data.get('msg', 'Unknown error')
            api_error = BinanceAPIException(
                code=error_code,
                message=error_message,
            )
        except asyncio.CancelledError as error:
            await self._observe_preserving_error(
                "request_cancelled",
                self._observer_details(
                    method=method,
                    path=path,
                    params=params,
                    started_at=started_at,
                    response=response,
                    response_body=response_body,
                    error=error,
                ),
                error,
            )
            raise
        except Exception as error:
            request_error = RuntimeError(f"Request failed: {error}")
            await self._observe_preserving_error(
                "request_failed",
                self._observer_details(
                    method=method,
                    path=path,
                    params=params,
                    started_at=started_at,
                    response=response,
                    response_body=response_body,
                    error=error,
                ),
                request_error,
            )
            raise request_error from error
        await self._observe_preserving_error(
            "request_failed",
            self._observer_details(
                method=method,
                path=path,
                params=params,
                started_at=started_at,
                response=response,
                response_body=error_data,
                error=api_error,
            ),
            api_error,
        )
        raise api_error

    # ========== 订单接口 ==========

    async def get_exchange_info(self) -> dict[str, Any]:
        """读取 USD-M Futures 交易规则；该接口无需签名。"""
        return await self._request('GET', '/fapi/v1/exchangeInfo', {}, signed=False)

    async def get_open_interest_history(
        self, symbol: str, *, period: str = "5m", limit: int = 2
    ) -> list[dict[str, Any]]:
        """Read public USD-M aggregate open-interest history."""
        if not symbol:
            raise ValueError("symbol is required")
        if period != "5m":
            raise ValueError("only the 5m metrics period is supported")
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        result = await self._request(
            "GET",
            "/futures/data/openInterestHist",
            {"symbol": symbol, "period": period, "limit": limit},
            signed=False,
        )
        if not isinstance(result, list):
            raise RuntimeError("invalid Binance open-interest history response")
        return result

    async def get_global_long_short_account_ratio(
        self, symbol: str, *, period: str = "5m", limit: int = 2
    ) -> list[dict[str, Any]]:
        """Read public USD-M global long/short account-ratio history."""
        if not symbol:
            raise ValueError("symbol is required")
        if period != "5m":
            raise ValueError("only the 5m metrics period is supported")
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        result = await self._request(
            "GET",
            "/futures/data/globalLongShortAccountRatio",
            {"symbol": symbol, "period": period, "limit": limit},
            signed=False,
        )
        if not isinstance(result, list):
            raise RuntimeError("invalid Binance long/short ratio response")
        return result

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

    async def test_order(
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
        """Validate a signed order without sending it to the matching engine."""
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
        result = await self._request('POST', '/fapi/v1/order/test', params)
        if not isinstance(result, dict):
            raise RuntimeError("invalid Binance test order response")
        return result

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

    async def get_income_history(
        self,
        *,
        symbol: str | None = None,
        income_type: str | None = None,
        start_time: int | None = None,
        end_time: int | None = None,
        page: int | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """查询账户收入历史，包括资金费等账户事实。"""
        if symbol is not None and not symbol.strip():
            raise ValueError("symbol must not be blank")
        if income_type is not None and not income_type.strip():
            raise ValueError("income_type must not be blank")
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        if page is not None and page < 1:
            raise ValueError("page must be positive")
        if start_time is not None and start_time < 0:
            raise ValueError("start_time must be non-negative")
        if end_time is not None and end_time < 0:
            raise ValueError("end_time must be non-negative")
        if start_time is not None and end_time is not None and start_time > end_time:
            raise ValueError("start_time must not be after end_time")
        params: dict[str, Any] = {"limit": limit}
        if symbol is not None:
            params["symbol"] = symbol
        if income_type is not None:
            params["incomeType"] = income_type
        if start_time is not None:
            params["startTime"] = start_time
        if end_time is not None:
            params["endTime"] = end_time
        if page is not None:
            params["page"] = page
        result = await self._request("GET", "/fapi/v1/income", params)
        if not isinstance(result, list):
            raise RuntimeError("invalid Binance income history response")
        return result

    async def get_agg_trades(
        self,
        symbol: str,
        *,
        from_id: int | None = None,
        start_time: int | None = None,
        end_time: int | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """查询公开归集成交（aggTrades）。

        Binance 不允许 ``fromId`` 与时间范围参数同时使用。
        """
        if not symbol:
            raise ValueError("symbol is required")
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        if from_id is not None and (start_time is not None or end_time is not None):
            raise ValueError("from_id cannot be combined with start_time or end_time")
        if from_id is not None and from_id < 0:
            raise ValueError("from_id must be non-negative")
        if start_time is not None and start_time < 0:
            raise ValueError("start_time must be non-negative")
        if end_time is not None and end_time < 0:
            raise ValueError("end_time must be non-negative")
        if start_time is not None and end_time is not None and start_time > end_time:
            raise ValueError("start_time must not be after end_time")
        if (
            start_time is not None and end_time is not None
            and end_time - start_time > 3_600_000
        ):
            raise ValueError("aggTrade time range must not exceed one hour")

        params: dict[str, Any] = {"symbol": symbol, "limit": limit}
        if from_id is not None:
            params["fromId"] = from_id
        if start_time is not None:
            params["startTime"] = start_time
        if end_time is not None:
            params["endTime"] = end_time

        result = await self._request(
            'GET', '/fapi/v1/aggTrades', params, signed=False
        )
        if not isinstance(result, list):
            raise RuntimeError("invalid Binance aggregate trades response")
        return result

    async def get_klines(
        self,
        symbol: str,
        interval: str,
        *,
        limit: int = 500,
        start_time: int | None = None,
        end_time: int | None = None,
    ) -> list[list[Any]]:
        """读取公开的已完成 K 线候选数据，供实时策略启动预热。"""
        if not symbol:
            raise ValueError("symbol is required")
        if not interval:
            raise ValueError("interval is required")
        if not 1 <= limit <= 1500:
            raise ValueError("limit must be between 1 and 1500")
        if start_time is not None and end_time is not None and start_time > end_time:
            raise ValueError("start_time must not be after end_time")
        if start_time is not None and start_time < 0:
            raise ValueError("start_time must be non-negative")
        if end_time is not None and end_time < 0:
            raise ValueError("end_time must be non-negative")
        params: dict[str, Any] = {
            'symbol': symbol,
            'interval': interval,
            'limit': limit,
        }
        if start_time is not None:
            params['startTime'] = start_time
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
