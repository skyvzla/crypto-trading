"""Best-effort Redis Stream wakeups for the PostgreSQL-backed worker."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol


logger = logging.getLogger(__name__)


class RedisStreamClient(Protocol):
    async def xadd(self, name: str, fields: dict[str, str], **kwargs: Any) -> Any: ...

    async def xread(self, streams: dict[str, str], **kwargs: Any) -> Any: ...


class RedisWakeup:
    """Use Redis only to shorten latency; a timeout always returns to DB polling."""

    DEFAULT_STREAM = "notifications:wakeup:v1"

    def __init__(
        self,
        redis_client: RedisStreamClient,
        *,
        stream: str = DEFAULT_STREAM,
        maxlen: int = 1000,
    ) -> None:
        self._redis = redis_client
        self._stream = stream
        self._maxlen = maxlen
        self._last_id = "$"
        self._redis_available = True

    async def notify(self, event_id: str) -> bool:
        """Publish a disposable wakeup after the event transaction commits."""

        try:
            await self._redis.xadd(
                self._stream,
                {"event_id": str(event_id)},
                maxlen=self._maxlen,
                approximate=True,
            )
        except Exception as exc:
            self._log_unavailable(exc)
            return False
        self._log_recovered()
        return True

    async def wait(self, timeout_seconds: float) -> bool:
        """Wait for one wakeup; Redis failure degrades to a timed DB poll."""

        timeout_seconds = max(0.0, timeout_seconds)
        block_ms = max(1, int(timeout_seconds * 1000))
        try:
            messages = await self._redis.xread(
                {self._stream: self._last_id},
                count=1,
                block=block_ms,
            )
        except Exception as exc:
            self._log_unavailable(exc)
            if timeout_seconds:
                await asyncio.sleep(timeout_seconds)
            return False
        self._log_recovered()
        message_id = _last_message_id(messages)
        if message_id is None:
            return False
        self._last_id = message_id
        return True

    def _log_unavailable(self, exc: Exception) -> None:
        if self._redis_available:
            logger.warning(
                "notification Redis wakeup unavailable; using DB polling: %s",
                exc,
            )
        self._redis_available = False

    def _log_recovered(self) -> None:
        if not self._redis_available:
            logger.info("notification Redis wakeup recovered")
        self._redis_available = True


class PollingWakeup:
    """Fallback used when Redis is intentionally not configured."""

    async def notify(self, event_id: str) -> bool:
        return False

    async def wait(self, timeout_seconds: float) -> bool:
        if timeout_seconds > 0:
            await asyncio.sleep(timeout_seconds)
        return False


def _last_message_id(messages: Any) -> str | None:
    if not messages:
        return None
    try:
        entries = messages[-1][1]
        message_id = entries[-1][0]
    except (IndexError, KeyError, TypeError):
        return None
    if isinstance(message_id, bytes):
        return message_id.decode("utf-8")
    return str(message_id)
