"""At-least-once notification delivery worker."""

from __future__ import annotations

import asyncio
import inspect
import logging
import random
import socket
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from trading_platform.notifications.adapters import (
    AdapterRegistry,
    AdapterRequest,
    DeliveryError,
    DeliveryReceipt,
    PermanentDeliveryError,
    RetryableDeliveryError,
)
from trading_platform.notifications.domain import DeliveryClaim
from trading_platform.notifications.wakeup import PollingWakeup


logger = logging.getLogger(__name__)


class DeliveryRepository(Protocol):
    async def route_pending_events(self, limit: int = 100) -> int: ...

    async def claim_deliveries(
        self, worker_id: str, *, limit: int, lease_seconds: int
    ) -> Sequence[DeliveryClaim]: ...

    async def mark_delivery_sent(
        self,
        delivery_id: object,
        worker_id: str,
        *,
        provider_message_id: str | None,
    ) -> bool: ...

    async def mark_delivery_retry(
        self,
        delivery_id: object,
        worker_id: str,
        *,
        error: str,
        next_attempt_at: datetime,
    ) -> bool: ...

    async def mark_delivery_dead(
        self, delivery_id: object, worker_id: str, *, error: str
    ) -> bool: ...


class EventBridge(Protocol):
    async def run_once(self, *, now: datetime | None = None) -> int: ...


@dataclass(frozen=True)
class WorkerStats:
    routed: int = 0
    claimed: int = 0
    sent: int = 0
    retried: int = 0
    dead: int = 0
    lease_lost: int = 0

    def add(self, other: "WorkerStats") -> "WorkerStats":
        return WorkerStats(
            routed=self.routed + other.routed,
            claimed=self.claimed + other.claimed,
            sent=self.sent + other.sent,
            retried=self.retried + other.retried,
            dead=self.dead + other.dead,
            lease_lost=self.lease_lost + other.lease_lost,
        )


class NotificationWorker:
    """Claim leased rows, send independently, then commit a terminal state."""

    def __init__(
        self,
        repository: DeliveryRepository,
        adapters: AdapterRegistry,
        *,
        wakeup: Any | None = None,
        worker_id: str | None = None,
        concurrency: int = 8,
        batch_size: int = 100,
        route_batch_size: int = 100,
        lease_seconds: int = 60,
        max_attempts: int = 5,
        base_retry_seconds: float = 5.0,
        max_retry_seconds: float = 3600.0,
        jitter_ratio: float = 0.2,
        poll_interval_seconds: float = 5.0,
        now: Callable[[], datetime] | None = None,
        random_value: Callable[[], float] | None = None,
        source_bridge: EventBridge | None = None,
    ) -> None:
        if concurrency < 1:
            raise ValueError("concurrency must be positive")
        if batch_size < 1 or route_batch_size < 1:
            raise ValueError("batch sizes must be positive")
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if base_retry_seconds < 0 or max_retry_seconds < 0:
            raise ValueError("retry delays cannot be negative")
        if not 0 <= jitter_ratio <= 1:
            raise ValueError("jitter_ratio must be between zero and one")
        self.repository = repository
        self.adapters = (
            AdapterRegistry(adapters)
            if isinstance(adapters, Mapping)
            else adapters
        )
        self.wakeup = wakeup or PollingWakeup()
        self.worker_id = worker_id or _default_worker_id()
        self.concurrency = concurrency
        self.batch_size = batch_size
        self.route_batch_size = route_batch_size
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts
        self.base_retry_seconds = base_retry_seconds
        self.max_retry_seconds = max_retry_seconds
        self.jitter_ratio = jitter_ratio
        self.poll_interval_seconds = max(0.0, poll_interval_seconds)
        self._now = now or (lambda: datetime.now(UTC))
        self._random_value = random_value or random.random
        self.source_bridge = source_bridge
        self._stop = asyncio.Event()

    async def run_once(self) -> WorkerStats:
        """Route pending events and drain one bounded batch of due deliveries."""

        routed = 0
        if self.source_bridge is not None:
            try:
                await self.source_bridge.run_once(now=self._now())
            except Exception:
                # Domain facts remain available for the next cycle; an alert bridge
                # outage must never stop already persisted deliveries.
                logger.exception("notification domain event bridge failed")
        try:
            routed = int(
                await self.repository.route_pending_events(self.route_batch_size)
            )
        except Exception:
            # Existing due deliveries remain safe to process if routing is temporarily
            # unavailable; the next polling cycle retries the route transaction.
            logger.exception("notification event routing failed")

        try:
            claims = list(
                await self.repository.claim_deliveries(
                    self.worker_id,
                    limit=self.batch_size,
                    lease_seconds=self.lease_seconds,
                )
            )
        except Exception:
            logger.exception("notification delivery claim failed")
            return WorkerStats(routed=routed)

        if not claims:
            return WorkerStats(routed=routed)
        semaphore = asyncio.Semaphore(self.concurrency)

        async def process(claim: DeliveryClaim) -> WorkerStats:
            async with semaphore:
                return await self._process_claim(claim)

        results = await asyncio.gather(*(process(claim) for claim in claims))
        stats = WorkerStats(routed=routed, claimed=len(claims))
        for result in results:
            stats = stats.add(result)
        return stats

    async def run(self) -> None:
        """Run until ``stop`` is called; wakeup failure naturally becomes polling."""

        self._stop.clear()
        while not self._stop.is_set():
            stats = await self.run_once()
            if self._stop.is_set():
                break
            if stats.claimed >= self.batch_size or stats.routed >= self.route_batch_size:
                continue
            wake_task = asyncio.create_task(
                self.wakeup.wait(self.poll_interval_seconds)
            )
            stop_task = asyncio.create_task(self._stop.wait())
            done, pending = await asyncio.wait(
                {wake_task, stop_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                # Surface unexpected wakeup implementation failures; RedisWakeup itself
                # converts Redis errors to a delayed False result.
                task.result()

    def stop(self) -> None:
        self._stop.set()

    async def aclose(self) -> None:
        """Close adapters that own HTTP clients without closing shared clients."""

        for adapter in _iter_adapters(self.adapters):
            close = getattr(adapter, "aclose", None)
            if close is None:
                continue
            result = close()
            if inspect.isawaitable(result):
                await result

    async def _process_claim(self, claim: DeliveryClaim) -> WorkerStats:
        now = _aware(self._now())
        delivery = claim.delivery
        event = claim.event
        attempt_count = _attempt_count(delivery)
        expires_at = getattr(event, "expires_at", None)
        if expires_at is not None and _aware(expires_at) <= now:
            return await self._dead_result(
                delivery.id,
                "event expired",
                expected_attempt_count=attempt_count,
            )
        if attempt_count is not None and attempt_count > self.max_attempts:
            return await self._dead_result(
                delivery.id,
                "maximum delivery attempts exceeded",
                expected_attempt_count=attempt_count,
            )

        try:
            request = _claim_request(claim)
        except Exception as exc:
            return await self._dead_result(
                delivery.id,
                f"invalid delivery snapshot: {type(exc).__name__}",
                expected_attempt_count=attempt_count,
            )
        try:
            receipt = await self.adapters.send(request)
        except PermanentDeliveryError as exc:
            return await self._dead_result(
                delivery.id,
                _error_text(exc),
                expected_attempt_count=attempt_count,
            )
        except RetryableDeliveryError as exc:
            return await self._handle_retry(delivery, event, exc, now)
        except DeliveryError as exc:
            # Custom adapters may use DeliveryError directly; its default is permanent.
            if exc.retryable:
                return await self._handle_retry(delivery, event, exc, now)
            return await self._dead_result(
                delivery.id,
                _error_text(exc),
                expected_attempt_count=attempt_count,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("unexpected notification adapter failure")
            return await self._handle_retry(
                delivery,
                event,
                RetryableDeliveryError(
                    f"adapter failure: {type(exc).__name__}"
                ),
                now,
            )

        marked = await self._mark_sent(
            delivery.id,
            provider_message_id=_receipt_id(receipt),
            expected_attempt_count=attempt_count,
        )
        return WorkerStats(sent=1 if marked else 0, lease_lost=0 if marked else 1)

    async def _handle_retry(
        self,
        delivery: Any,
        event: Any,
        error: RetryableDeliveryError,
        now: datetime,
    ) -> WorkerStats:
        attempt = max(1, int(getattr(delivery, "attempt_count", 1)))
        if attempt >= self.max_attempts:
            return await self._dead_result(
                delivery.id,
                _error_text(error),
                expected_attempt_count=attempt,
            )

        delay = error.retry_after
        if delay is None:
            delay = min(
                self.max_retry_seconds,
                self.base_retry_seconds * (2 ** max(0, attempt - 1)),
            )
            if delay > 0 and self.jitter_ratio:
                delay += delay * self.jitter_ratio * self._random_value()
        delay = max(0.0, min(float(delay), self.max_retry_seconds))
        next_attempt_at = now + timedelta(seconds=delay)
        expires_at = getattr(event, "expires_at", None)
        if expires_at is not None and next_attempt_at >= _aware(expires_at):
            return await self._dead_result(
                delivery.id,
                f"{_error_text(error)}; event expires before retry",
                expected_attempt_count=attempt,
            )
        marked = await self._mark_retry(
            delivery.id,
            error=_error_text(error),
            next_attempt_at=next_attempt_at,
            expected_attempt_count=attempt,
        )
        return WorkerStats(retried=1 if marked else 0, lease_lost=0 if marked else 1)

    async def _mark_dead(
        self,
        delivery_id: object,
        error: str,
        *,
        expected_attempt_count: int | None = None,
    ) -> int:
        try:
            method = self.repository.mark_delivery_dead
            marked = await method(
                delivery_id,
                self.worker_id,
                **_supported_kwargs(
                    method,
                    last_error=error,
                    error=error,
                    expected_attempt_count=expected_attempt_count,
                ),
            )
        except Exception:
            logger.exception("notification dead-letter state update failed")
            return 0
        return 1 if marked else 0

    async def _dead_result(
        self,
        delivery_id: object,
        error: str,
        *,
        expected_attempt_count: int | None = None,
    ) -> WorkerStats:
        marked = await self._mark_dead(
            delivery_id,
            error,
            expected_attempt_count=expected_attempt_count,
        )
        return WorkerStats(
            dead=1 if marked else 0,
            lease_lost=0 if marked else 1,
        )

    async def _mark_sent(
        self,
        delivery_id: object,
        *,
        provider_message_id: str | None,
        expected_attempt_count: int | None = None,
    ) -> bool:
        try:
            method = self.repository.mark_delivery_sent
            return bool(
                await method(
                    delivery_id,
                    self.worker_id,
                    **_supported_kwargs(
                        method,
                        provider_message_id=provider_message_id,
                        expected_attempt_count=expected_attempt_count,
                    ),
                )
            )
        except Exception:
            logger.exception("notification sent state update failed")
            return False

    async def _mark_retry(
        self,
        delivery_id: object,
        *,
        error: str,
        next_attempt_at: datetime,
        expected_attempt_count: int | None = None,
    ) -> bool:
        try:
            method = self.repository.mark_delivery_retry
            return bool(
                await method(
                    delivery_id,
                    self.worker_id,
                    **_supported_kwargs(
                        method,
                        next_attempt_at=next_attempt_at,
                        last_error=error,
                        error=error,
                        expected_attempt_count=expected_attempt_count,
                    ),
                )
            )
        except Exception:
            logger.exception("notification retry state update failed")
            return False


def _claim_request(claim: DeliveryClaim) -> AdapterRequest:
    event = claim.event
    payload = getattr(event, "payload", {})
    return AdapterRequest(
        delivery_id=str(claim.delivery.id),
        event_id=str(event.id),
        event_type=str(event.event_type),
        severity=str(event.severity),
        source=str(event.source),
        title=str(event.title),
        body=str(event.body),
        payload=payload if isinstance(payload, Mapping) else {},
        occurred_at=event.occurred_at,
        connector=claim.connector,
        endpoint=claim.endpoint,
    )


def _iter_adapters(registry: Any) -> list[Any]:
    values = getattr(registry, "_adapters", None)
    if isinstance(values, Mapping):
        result: list[Any] = []
        seen: set[int] = set()
        for adapter in values.values():
            identity = id(adapter)
            if identity not in seen:
                seen.add(identity)
                result.append(adapter)
        return result
    return [registry]


def _receipt_id(receipt: DeliveryReceipt | Any) -> str | None:
    value = getattr(receipt, "provider_message_id", None)
    return None if value is None else str(value)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _error_text(error: BaseException) -> str:
    text = str(error).strip() or type(error).__name__
    return text[:1000]


def _attempt_count(delivery: Any) -> int | None:
    try:
        value = int(delivery.attempt_count)
    except (AttributeError, TypeError, ValueError):
        return None
    return value


def _supported_kwargs(method: Callable[..., Any], **kwargs: Any) -> dict[str, Any]:
    """Keep the worker compatible with small fake repositories used by tests."""

    try:
        parameters = inspect.signature(method).parameters
    except (TypeError, ValueError):
        return kwargs
    if any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()):
        return kwargs
    return {key: value for key, value in kwargs.items() if key in parameters}


def _default_worker_id() -> str:
    try:
        host = socket.gethostname()
    except OSError:
        host = "unknown"
    return f"notification-worker:{host}:{uuid.uuid4()}"
