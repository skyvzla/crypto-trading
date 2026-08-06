"""
Binance API 限速器
使用滑动窗口算法，严格遵循 Binance 限速规则
"""
import asyncio
import time
from collections import deque
from dataclasses import dataclass
from typing import Literal


@dataclass
class RateLimitRule:
    """限速规则"""
    interval: int  # 时间窗口（秒）
    limit: int     # 窗口内最大请求数/权重


class RateLimiter:
    """
    滑动窗口限速器

    Binance Futures API 限速规则：
    - 1200 请求/分钟（按权重计算）
    - 每个接口权重不同：GET /fapi/v1/order = 2, POST /fapi/v1/order = 1
    """

    def __init__(self, rules: list[RateLimitRule]):
        """
        Args:
            rules: 限速规则列表，例如 [RateLimitRule(60, 1200)]
        """
        self.rules = rules
        # 每个规则维护一个时间戳队列
        self.windows: dict[int, deque[float]] = {
            rule.interval: deque() for rule in rules
        }
        self._lock = asyncio.Lock()

    async def acquire(self, weight: int = 1) -> None:
        """
        请求权重配额，如果超限则等待

        Args:
            weight: 请求权重（默认1）
        """
        async with self._lock:
            now = time.time()

            # 检查所有规则
            max_wait = 0.0
            for rule in self.rules:
                window = self.windows[rule.interval]

                # 清理过期记录
                cutoff = now - rule.interval
                while window and window[0] < cutoff:
                    window.popleft()

                # 计算当前窗口权重
                current_weight = len(window)  # 简化：每个请求算1个权重

                # 如果加上本次请求会超限
                if current_weight + weight > rule.limit:
                    # 计算需要等待的时间（最老请求过期的时间）
                    if window:
                        oldest = window[0]
                        wait_time = oldest + rule.interval - now
                        max_wait = max(max_wait, wait_time)

            # 如果需要等待
            if max_wait > 0:
                await asyncio.sleep(max_wait)
                now = time.time()

            # 记录本次请求
            for rule in self.rules:
                window = self.windows[rule.interval]
                for _ in range(weight):
                    window.append(now)

    def reset(self) -> None:
        """重置所有窗口（测试用）"""
        for window in self.windows.values():
            window.clear()


# 预定义限速器实例
# Binance Futures 默认限制：1200 请求权重/分钟
DEFAULT_RATE_LIMITER = RateLimiter([
    RateLimitRule(interval=60, limit=1200)
])


# 接口权重映射
ENDPOINT_WEIGHTS: dict[tuple[str, str], int] = {
    # (method, path_prefix) -> weight
    ('POST', '/fapi/v1/order'): 1,
    ('DELETE', '/fapi/v1/order'): 1,
    ('GET', '/fapi/v1/order'): 2,
    ('GET', '/fapi/v2/account'): 5,
    ('GET', '/fapi/v2/positionRisk'): 5,
    ('GET', '/fapi/v1/openOrders'): 40,
    ('POST', '/fapi/v1/listenKey'): 1,
    ('PUT', '/fapi/v1/listenKey'): 1,
    ('DELETE', '/fapi/v1/listenKey'): 1,
}


def get_endpoint_weight(method: str, path: str) -> int:
    """
    获取接口权重

    Args:
        method: HTTP 方法
        path: 请求路径

    Returns:
        权重值（默认1）
    """
    for (m, prefix), weight in ENDPOINT_WEIGHTS.items():
        if method == m and path.startswith(prefix):
            return weight
    return 1
