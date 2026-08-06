"""
测试 Binance 执行层基础实现
"""
import asyncio
from decimal import Decimal

import pytest

from trading_platform.shared.binance import (
    BinanceRestClient,
    BinanceAPIException,
    RateLimiter,
    RateLimitRule,
)


class TestRateLimiter:
    """测试限速器"""

    @pytest.mark.asyncio
    async def test_single_request_passes(self):
        """单个请求应该立即通过"""
        limiter = RateLimiter([RateLimitRule(interval=60, limit=10)])

        start = asyncio.get_event_loop().time()
        await limiter.acquire(1)
        elapsed = asyncio.get_event_loop().time() - start

        assert elapsed < 0.1  # 应该立即返回

    @pytest.mark.asyncio
    async def test_exceeding_limit_waits(self):
        """超过限制应该等待"""
        limiter = RateLimiter([RateLimitRule(interval=1, limit=2)])

        # 发送 2 个请求（达到限制）
        await limiter.acquire(1)
        await limiter.acquire(1)

        # 第 3 个请求应该等待
        start = asyncio.get_event_loop().time()
        await limiter.acquire(1)
        elapsed = asyncio.get_event_loop().time() - start

        assert elapsed >= 0.9  # 应该等待约 1 秒

    @pytest.mark.asyncio
    async def test_weight_calculation(self):
        """权重计算测试"""
        limiter = RateLimiter([RateLimitRule(interval=1, limit=5)])

        # 发送权重为 3 的请求
        await limiter.acquire(3)

        # 再发送权重为 2 的请求（达到限制）
        await limiter.acquire(2)

        # 下一个请求应该等待
        start = asyncio.get_event_loop().time()
        await limiter.acquire(1)
        elapsed = asyncio.get_event_loop().time() - start

        assert elapsed >= 0.9


class TestBinanceRestClient:
    """测试 REST 客户端（需要 mock，这里只测试签名）"""

    def test_sign_adds_timestamp_and_signature(self):
        """测试签名函数"""
        client = BinanceRestClient(
            api_key="test_key",
            api_secret="test_secret",
        )

        params = {"symbol": "BTCUSDT", "side": "BUY"}
        signed = client._sign(params)

        assert "timestamp" in signed
        assert "signature" in signed
        assert signed["symbol"] == "BTCUSDT"
        assert signed["side"] == "BUY"

    @pytest.mark.asyncio
    async def test_client_creation_and_close(self):
        """测试客户端创建和关闭"""
        client = BinanceRestClient(
            api_key="test_key",
            api_secret="test_secret",
        )

        assert client.api_key == "test_key"
        assert client.base_url == "https://fapi.binance.com"

        await client.close()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
