"""Notification configuration, event, and delivery domain values."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from fnmatch import fnmatchcase
from typing import Any
from uuid import UUID


class ConnectorType(StrEnum):
    TELEGRAM = "telegram"
    WEBHOOK = "webhook"


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class RoutingStatus(StrEnum):
    PENDING = "pending"
    ROUTED = "routed"
    SUPPRESSED = "suppressed"
    UNROUTED = "unrouted"
    TARGETED = "targeted"


class DeliveryStatus(StrEnum):
    PENDING = "pending"
    SENDING = "sending"
    RETRY = "retry"
    SENT = "sent"
    DEAD = "dead"


@dataclass(frozen=True)
class NotificationConnector:
    id: UUID
    name: str
    type: ConnectorType
    secret_ref: str | None
    config: dict[str, Any]
    enabled: bool
    version: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class NotificationEndpoint:
    id: UUID
    connector_id: UUID
    name: str
    address: str
    config: dict[str, Any]
    enabled: bool
    version: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class NotificationGroup:
    id: UUID
    name: str
    description: str | None
    enabled: bool
    version: int
    endpoint_ids: tuple[UUID, ...]
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class NotificationPolicy:
    id: UUID
    name: str
    event_pattern: str
    severity: Severity
    priority: int
    suppress: bool
    enabled: bool
    version: int
    group_ids: tuple[UUID, ...]
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class NotificationEvent:
    id: UUID
    event_type: str
    severity: Severity
    source: str
    title: str
    body: str
    payload: dict[str, Any]
    idempotency_key: str
    correlation_id: str | None
    fingerprint: str | None
    matched_policy_id: UUID | None
    routing_status: RoutingStatus
    occurred_at: datetime
    expires_at: datetime | None
    created_at: datetime


@dataclass(frozen=True)
class NotificationDelivery:
    id: UUID
    event_id: UUID
    endpoint_id: UUID
    connector_snapshot: dict[str, Any]
    endpoint_snapshot: dict[str, Any]
    status: DeliveryStatus
    attempt_count: int
    next_attempt_at: datetime
    lease_until: datetime | None
    lease_owner: str | None
    last_error: str | None
    provider_message_id: str | None
    created_at: datetime
    updated_at: datetime
    sent_at: datetime | None


@dataclass(frozen=True)
class PublishResult:
    event: NotificationEvent
    deliveries: tuple[NotificationDelivery, ...]
    created: bool


@dataclass(frozen=True)
class DeliveryClaim:
    delivery: NotificationDelivery
    event: NotificationEvent
    connector: dict[str, Any]
    endpoint: dict[str, Any]


def event_pattern_matches(pattern: str, event_type: str) -> bool:
    """Match event patterns using Python's standard shell-style glob rules."""
    return fnmatchcase(event_type, pattern)


def choose_policy(
    policies: list[NotificationPolicy], event_type: str
) -> NotificationPolicy | None:
    """Choose one deterministic policy: exact before glob, then priority."""
    matches = [
        policy
        for policy in policies
        if policy.enabled and event_pattern_matches(policy.event_pattern, event_type)
    ]
    if not matches:
        return None
    return min(
        matches,
        key=lambda policy: (
            policy.event_pattern != event_type,
            -policy.priority,
            policy.created_at,
            str(policy.id),
        ),
    )
