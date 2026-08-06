"""
FastAPI 订阅管理路由
提供声明式订阅接口和健康检查
"""
import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator


logger = logging.getLogger(__name__)


# ==================== 请求/响应模型 ====================


class SubscriptionRequest(BaseModel):
    """订阅请求"""
    symbols: list[str] = Field(..., min_length=1, description="交易对列表")
    types: list[str] = Field(..., min_length=1, description="订阅类型列表")

    @field_validator("types")
    @classmethod
    def validate_subscription_types(cls, values: list[str]) -> list[str]:
        for value in values:
            if value == "bar1s":
                continue
            if value.startswith("kline:") and value.split(":", 1)[1]:
                continue
            raise ValueError(f"不支持的订阅类型: {value}")
        return values


class SubscriptionResponse(BaseModel):
    """订阅响应"""
    status: str = "ok"
    consumer_id: str
    subscribed: dict[str, list[str]]
    active_streams: int


class UnsubscribeResponse(BaseModel):
    """取消订阅响应"""
    status: str = "ok"
    consumer_id: str
    unsubscribed: str = "all"


class HealthResponse(BaseModel):
    """健康检查响应"""
    status: str = "ready"
    instance_epoch: str
    uptime_seconds: float
    binance_testnet: bool = False
    subscribed_symbols: int
    active_ws_streams: int
    redis_connected: bool = False
    websocket_connected: bool = False
    connection_generation: int = 0
    data_quality_ready: bool = False
    data_quality_issues: int = 0
    pubsub_delivery_ready: bool = True
    pubsub_delivery_issues: int = 0


class QualityResponse(BaseModel):
    """策略可消费的逐流数据质量状态。"""

    ready: bool
    websocket_connected: bool
    connection_generation: int
    last_connected_at_ms: int | None
    last_disconnected_at_ms: int | None
    streams: dict[str, dict[str, Any]]
    pubsub_delivery_ready: bool = True
    pubsub_delivery_issues: int = 0
    pubsub_channels: dict[str, dict[str, Any]] = Field(default_factory=dict)


# ==================== 订阅管理器 ====================


class SubscriptionManager:
    """
    订阅管理器

    维护消费者订阅状态和引用计数
    管理 WebSocket 流的生命周期
    """

    def __init__(self, instance_epoch: str):
        self.instance_epoch = instance_epoch

        # {consumer_id: {symbol: [types]}}
        self.consumers: dict[str, dict[str, list[str]]] = {}

        # {symbol: {type: refcount}}
        self.refcounts: dict[str, dict[str, int]] = {}

        # {(symbol, type): actual_stream_name}
        self.stream_mapping: dict[tuple[str, str], str] = {}

    def update_subscription(
        self,
        consumer_id: str,
        symbols: list[str],
        types: list[str],
    ) -> dict[str, Any]:
        """
        更新消费者订阅（声明式，幂等）

        Returns:
            {
                "added": [(symbol, type), ...],
                "removed": [(symbol, type), ...],
                "subscribed": {symbol: [types]},
            }
        """
        # 获取旧订阅
        old_subscription = self.consumers.get(consumer_id, {})
        old_pairs = set()
        for sym, sub_types in old_subscription.items():
            for t in sub_types:
                old_pairs.add((sym, t))

        # 计算新订阅
        new_subscription: dict[str, list[str]] = {}
        new_pairs = set()

        for symbol in symbols:
            new_subscription[symbol] = list(types)
            for t in types:
                new_pairs.add((symbol, t))

        # 计算差异
        added = new_pairs - old_pairs
        removed = old_pairs - new_pairs

        # 更新引用计数
        for symbol, sub_type in removed:
            self._decrement_refcount(symbol, sub_type)

        for symbol, sub_type in added:
            self._increment_refcount(symbol, sub_type)

        # 更新消费者订阅
        self.consumers[consumer_id] = new_subscription

        logger.info(
            f"更新订阅: consumer={consumer_id}, "
            f"added={len(added)}, removed={len(removed)}"
        )

        return {
            "added": list(added),
            "removed": list(removed),
            "subscribed": new_subscription,
        }

    def remove_consumer(self, consumer_id: str) -> dict[str, Any]:
        """
        移除消费者的所有订阅

        Returns:
            {
                "removed": [(symbol, type), ...],
            }
        """
        old_subscription = self.consumers.pop(consumer_id, {})

        removed = []
        for symbol, sub_types in old_subscription.items():
            for sub_type in sub_types:
                self._decrement_refcount(symbol, sub_type)
                removed.append((symbol, sub_type))

        logger.info(f"移除消费者: consumer={consumer_id}, removed={len(removed)}")

        return {"removed": removed}

    def _increment_refcount(self, symbol: str, sub_type: str) -> None:
        """增加引用计数"""
        if symbol not in self.refcounts:
            self.refcounts[symbol] = {}

        if sub_type not in self.refcounts[symbol]:
            self.refcounts[symbol][sub_type] = 0

        self.refcounts[symbol][sub_type] += 1

    def _decrement_refcount(self, symbol: str, sub_type: str) -> None:
        """减少引用计数"""
        if symbol not in self.refcounts:
            return

        if sub_type not in self.refcounts[symbol]:
            return

        self.refcounts[symbol][sub_type] -= 1

        # 清理计数为 0 的条目
        if self.refcounts[symbol][sub_type] <= 0:
            del self.refcounts[symbol][sub_type]

        if not self.refcounts[symbol]:
            del self.refcounts[symbol]

    def get_active_streams(self) -> dict[str, dict[str, int]]:
        """获取所有活跃流及其引用计数"""
        return dict(self.refcounts)

    def get_stats(self) -> dict[str, Any]:
        """获取统计信息"""
        total_streams = sum(
            len(types) for types in self.refcounts.values()
        )

        return {
            "instance_epoch": self.instance_epoch,
            "consumers": len(self.consumers),
            "subscribed_symbols": len(self.refcounts),
            "active_streams": total_streams,
        }


# ==================== 路由定义 ====================


def create_router(subscription_manager: SubscriptionManager) -> APIRouter:
    """创建订阅管理路由"""

    router = APIRouter()

    @router.put("/subscriptions/{consumer_id}", response_model=SubscriptionResponse)
    async def update_subscription(
        consumer_id: str,
        request: SubscriptionRequest,
    ) -> SubscriptionResponse:
        """
        声明式订阅（幂等）

        每次提交完整的期望集合，行情层比对差异并更新
        """
        try:
            result = subscription_manager.update_subscription(
                consumer_id=consumer_id,
                symbols=request.symbols,
                types=request.types,
            )

            return SubscriptionResponse(
                consumer_id=consumer_id,
                subscribed=result["subscribed"],
                active_streams=sum(
                    len(types) for types in subscription_manager.refcounts.values()
                ),
            )

        except Exception as e:
            logger.error(f"更新订阅失败: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e))

    @router.delete("/subscriptions/{consumer_id}", response_model=UnsubscribeResponse)
    async def remove_subscription(consumer_id: str) -> UnsubscribeResponse:
        """注销消费者的所有订阅"""
        try:
            subscription_manager.remove_consumer(consumer_id)

            return UnsubscribeResponse(
                consumer_id=consumer_id,
            )

        except Exception as e:
            logger.error(f"移除订阅失败: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e))

    @router.get("/health", response_model=HealthResponse)
    async def health_check() -> HealthResponse:
        """健康检查，返回 instance_epoch 用于检测重启"""
        stats = subscription_manager.get_stats()

        return HealthResponse(
            instance_epoch=stats["instance_epoch"],
            uptime_seconds=0.0,  # TODO: 实现运行时长统计
            subscribed_symbols=stats["subscribed_symbols"],
            active_ws_streams=stats["active_streams"],
        )

    return router
