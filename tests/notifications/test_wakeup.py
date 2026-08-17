from __future__ import annotations

import pytest

from trading_platform.notifications.wakeup import PollingWakeup, RedisWakeup


class FakeRedis:
    def __init__(self):
        self.added = []
        self.reads = []
        self.fail_read = False
        self.fail_write = False

    async def xadd(self, name, fields, **kwargs):
        if self.fail_write:
            raise RuntimeError("redis down")
        self.added.append((name, fields, kwargs))
        return "1-0"

    async def xread(self, streams, **kwargs):
        self.reads.append((streams, kwargs))
        if self.fail_read:
            raise RuntimeError("redis down")
        return [[b"notifications:wakeup:v1", [(b"2-0", {b"event_id": b"event"})]]]


@pytest.mark.asyncio
async def test_redis_wakeup_stream_is_best_effort_and_tracks_cursor() -> None:
    redis = FakeRedis()
    wakeup = RedisWakeup(redis)

    assert await wakeup.notify("event-1") is True
    assert await wakeup.wait(0) is True
    assert await wakeup.wait(0) is True
    assert redis.added[0][0] == "notifications:wakeup:v1"
    assert redis.added[0][1] == {"event_id": "event-1"}
    assert redis.reads[0][0] == {"notifications:wakeup:v1": "$"}
    assert redis.reads[1][0] == {"notifications:wakeup:v1": "2-0"}


@pytest.mark.asyncio
async def test_redis_failure_returns_to_polling_without_raising() -> None:
    redis = FakeRedis()
    redis.fail_read = True
    redis.fail_write = True
    wakeup = RedisWakeup(redis)

    assert await wakeup.wait(0) is False
    assert await wakeup.notify("event-1") is False


@pytest.mark.asyncio
async def test_polling_wakeup_is_a_noop_for_publishers() -> None:
    wakeup = PollingWakeup()
    assert await wakeup.notify("event-1") is False
    assert await wakeup.wait(0) is False
