"""Redis 中的全局交易周期互斥状态。"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class CampaignLease:
    campaign_id: str
    strategy_id: str
    symbol: str
    started_at_ms: int
    origin_price: str | None = None
    origin_checked: bool = False
    reduced_at_origin: bool = False
    exit_requested: bool = False


class RedisCampaignStore:
    """用 Redis 原子 SET NX 保存唯一活跃交易周期。

    活跃周期不设置 TTL，避免持仓仍存在时锁自动过期。释放必须携带相同
    campaign_id，并由调用方在交易所订单和仓位均确认终态后执行。
    """

    def __init__(self, redis: Any, *, key: str = "trading_platform:campaign:active"):
        self.redis = redis
        self.key = key

    async def acquire(self, lease: CampaignLease) -> bool:
        if not all((lease.campaign_id, lease.strategy_id, lease.symbol)):
            raise ValueError("campaign_id, strategy_id and symbol are required")
        payload = json.dumps(asdict(lease), separators=(",", ":"), sort_keys=True)
        return bool(await self.redis.set(self.key, payload, nx=True))

    async def get_active(self) -> CampaignLease | None:
        raw = await self.redis.get(self.key)
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        data = json.loads(raw)
        return CampaignLease(**data)

    async def release(self, campaign_id: str) -> bool:
        if not campaign_id:
            raise ValueError("campaign_id is required")
        script = """
local raw = redis.call('GET', KEYS[1])
if not raw then return 0 end
local value = cjson.decode(raw)
if value.campaign_id ~= ARGV[1] then return 0 end
return redis.call('DEL', KEYS[1])
"""
        return bool(await self.redis.eval(script, 1, self.key, campaign_id))

    async def update_exit_state(
        self,
        campaign_id: str,
        *,
        origin_checked: bool,
        reduced_at_origin: bool,
        exit_requested: bool,
    ) -> bool:
        """只允许持有者原子更新 candidate 退出状态。"""
        script = """
local raw = redis.call('GET', KEYS[1])
if not raw then return 0 end
local value = cjson.decode(raw)
if value.campaign_id ~= ARGV[1] then return 0 end
value.origin_checked = ARGV[2] == '1'
value.reduced_at_origin = ARGV[3] == '1'
value.exit_requested = ARGV[4] == '1'
redis.call('SET', KEYS[1], cjson.encode(value))
return 1
"""
        return bool(
            await self.redis.eval(
                script,
                1,
                self.key,
                campaign_id,
                "1" if origin_checked else "0",
                "1" if reduced_at_origin else "0",
                "1" if exit_requested else "0",
            )
        )
