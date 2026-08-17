from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from trading_platform.notifications.adapters import (
    DeliveryReceipt,
    PermanentDeliveryError,
    RetryableDeliveryError,
)
from trading_platform.notifications.worker import NotificationWorker


NOW = datetime(2026, 8, 16, 8, tzinfo=UTC)


def claim(*, attempts: int = 1, expires_at=None):
    delivery = SimpleNamespace(id=uuid4(), attempt_count=attempts)
    event = SimpleNamespace(
        id=uuid4(),
        event_type="risk.halted",
        severity="critical",
        source="test",
        title="title",
        body="body",
        payload={"x": 1},
        occurred_at=NOW,
        expires_at=expires_at,
    )
    return SimpleNamespace(
        delivery=delivery,
        event=event,
        connector={"type": "fake"},
        endpoint={"address": "fake", "config": {}},
    )


class FakeRepository:
    def __init__(self, claims):
        self.claims = list(claims)
        self.calls = []

    async def route_pending_events(self, limit=100):
        self.calls.append(("route", limit))
        return 2

    async def claim_deliveries(self, worker_id, *, limit, lease_seconds):
        self.calls.append(("claim", worker_id, limit, lease_seconds))
        return self.claims

    async def mark_delivery_sent(
        self, delivery_id, worker_id, *, provider_message_id, expected_attempt_count=None
    ):
        self.calls.append(("sent", delivery_id, provider_message_id, expected_attempt_count))
        return True

    async def mark_delivery_retry(
        self,
        delivery_id,
        worker_id,
        *,
        error,
        next_attempt_at,
        expected_attempt_count=None,
    ):
        self.calls.append(("retry", delivery_id, error, next_attempt_at, expected_attempt_count))
        return True

    async def mark_delivery_dead(
        self, delivery_id, worker_id, *, error, expected_attempt_count=None
    ):
        self.calls.append(("dead", delivery_id, error, expected_attempt_count))
        return True


class FakeAdapters:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.requests = []

    async def send(self, request):
        self.requests.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class FakeBridge:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.calls = []

    async def run_once(self, *, now=None):
        self.calls.append(now)
        if self.fail:
            raise RuntimeError("bridge unavailable")
        return 1


@pytest.mark.asyncio
async def test_worker_sends_and_marks_each_claim_independently() -> None:
    first, second = claim(), claim()
    repository = FakeRepository([first, second])
    adapters = FakeAdapters(
        [DeliveryReceipt("provider-1"), PermanentDeliveryError("bad endpoint")]
    )
    worker = NotificationWorker(
        repository,
        adapters,
        worker_id="worker-1",
        concurrency=2,
        now=lambda: NOW,
        jitter_ratio=0,
    )

    stats = await worker.run_once()

    assert stats == stats.__class__(routed=2, claimed=2, sent=1, dead=1)
    assert any(
        call[0] == "sent" and call[2] == "provider-1" and call[3] == 1
        for call in repository.calls
    )
    assert any(call[0] == "dead" for call in repository.calls)
    assert len(adapters.requests) == 2


@pytest.mark.asyncio
async def test_worker_exponential_retry_honors_provider_delay() -> None:
    item = claim(attempts=2, expires_at=NOW + timedelta(minutes=5))
    repository = FakeRepository([item])
    adapters = FakeAdapters([RetryableDeliveryError("busy", retry_after=7)])
    worker = NotificationWorker(
        repository,
        adapters,
        worker_id="worker-1",
        now=lambda: NOW,
        max_attempts=5,
        jitter_ratio=0,
    )

    stats = await worker.run_once()

    assert stats.retried == 1
    retry_call = next(call for call in repository.calls if call[0] == "retry")
    assert retry_call[3] == NOW + timedelta(seconds=7)
    assert retry_call[4] == 2


@pytest.mark.asyncio
async def test_worker_dead_letters_after_max_attempts_or_event_expiry() -> None:
    maxed = claim(attempts=3)
    expired = claim(expires_at=NOW - timedelta(seconds=1))
    repository = FakeRepository([maxed, expired])
    adapters = FakeAdapters([RetryableDeliveryError("still down")])
    worker = NotificationWorker(
        repository,
        adapters,
        worker_id="worker-1",
        max_attempts=3,
        now=lambda: NOW,
        jitter_ratio=0,
    )

    stats = await worker.run_once()

    assert stats.dead == 2
    assert len(adapters.requests) == 1
    assert not any(call[0] == "retry" for call in repository.calls)


@pytest.mark.asyncio
async def test_worker_bridge_failure_does_not_drop_existing_deliveries() -> None:
    item = claim()
    repository = FakeRepository([item])
    adapters = FakeAdapters([DeliveryReceipt("provider")])
    bridge = FakeBridge(fail=True)
    worker = NotificationWorker(
        repository,
        adapters,
        source_bridge=bridge,
        worker_id="worker-1",
        now=lambda: NOW,
    )

    stats = await worker.run_once()

    assert stats.sent == 1
    assert len(bridge.calls) == 1
