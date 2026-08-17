from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from trading_platform.ledger.api.notifications import get_repository, router
from trading_platform.notifications.domain import (
    ConnectorType,
    DeliveryStatus,
    NotificationConnector,
    NotificationDelivery,
    NotificationEndpoint,
    NotificationEvent,
    NotificationGroup,
    NotificationPolicy,
    RoutingStatus,
    Severity,
)


def _now():
    return datetime.now(UTC)


class FakeNotificationRepository:
    def __init__(self):
        now = _now()
        self.connector = NotificationConnector(
            id=uuid4(), name="ops", type=ConnectorType.TELEGRAM,
            secret_ref="env:TG_OPS", config={"parse_mode": "HTML"}, enabled=True,
            version=1, created_at=now, updated_at=now,
        )
        self.endpoint = NotificationEndpoint(
            id=uuid4(), connector_id=self.connector.id, name="ops-chat",
            address="-1001", config={"thread_id": 7}, enabled=True, version=1,
            created_at=now, updated_at=now,
        )
        self.group = NotificationGroup(
            id=uuid4(), name="ops", description=None, enabled=True, version=1,
            endpoint_ids=(self.endpoint.id,), created_at=now, updated_at=now,
        )
        self.policy = NotificationPolicy(
            id=uuid4(), name="critical", event_pattern="risk.*",
            severity=Severity.CRITICAL, priority=10, suppress=False, enabled=True,
            version=1, group_ids=(self.group.id,), created_at=now, updated_at=now,
        )
        self.event = NotificationEvent(
            id=uuid4(), event_type="risk.halted", severity=Severity.CRITICAL,
            source="risk", title="halt", body="stopped", payload={"reason": "x"},
            idempotency_key="risk-1", correlation_id=None, fingerprint=None,
            matched_policy_id=self.policy.id, routing_status=RoutingStatus.ROUTED,
            occurred_at=now, expires_at=None, created_at=now,
        )
        self.delivery = NotificationDelivery(
            id=uuid4(), event_id=self.event.id, endpoint_id=self.endpoint.id,
            connector_snapshot={"type": "telegram", "secret_ref": "env:TG_OPS"},
            endpoint_snapshot={"address": "-1001"}, status=DeliveryStatus.PENDING,
            attempt_count=0, next_attempt_at=now, lease_until=None, lease_owner=None,
            last_error=None, provider_message_id=None, created_at=now, updated_at=now,
            sent_at=None,
        )

    async def create_connector(self, **kwargs):
        return self.connector

    async def get_connector(self, _id):
        return self.connector if _id == self.connector.id else None

    async def list_connectors(self, *, limit, offset):
        return [self.connector], 1

    async def publish_event(self, **kwargs):
        from trading_platform.notifications.domain import PublishResult
        return PublishResult(self.event, (self.delivery,), True)


@pytest.fixture
def api_app():
    app = FastAPI()
    app.include_router(router)
    repository = FakeNotificationRepository()
    app.dependency_overrides[get_repository] = lambda: repository
    return app, repository


@pytest.mark.asyncio
async def test_notification_connector_crud_and_publish_shape(api_app):
    app, repository = api_app
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        listed = await client.get("/api/v1/notifications/connectors")
        created = await client.post(
            "/api/v1/notifications/connectors",
            json={
                "name": "ops",
                "type": "telegram",
                "secret_ref": "env:TG_OPS",
                "config": {"parse_mode": "HTML"},
            },
        )
        published = await client.post(
            "/api/v1/notifications/events",
            headers={"Idempotency-Key": "risk-1"},
            json={
                "event_type": "risk.halted",
                "severity": "critical",
                "source": "risk",
                "title": "halt",
                "body": "stopped",
                "payload": {"reason": "x"},
            },
        )

    assert listed.status_code == 200
    assert listed.json()["items"][0]["secret_ref"] == "env:TG_OPS"
    assert created.status_code == 201
    assert published.status_code == 202
    assert published.json()["event"]["routing_status"] == "routed"
    assert len(published.json()["deliveries"]) == 1


@pytest.mark.asyncio
async def test_notification_config_rejects_credentials(api_app):
    app, _ = api_app
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/notifications/connectors",
            json={
                "name": "unsafe",
                "type": "telegram",
                "config": {"bot_token": "123:secret"},
            },
        )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_notification_publish_rejects_expired_event(api_app):
    app, _ = api_app
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/notifications/events",
            json={
                "event_type": "risk.halted",
                "severity": "critical",
                "source": "risk",
                "title": "halt",
                "body": "stopped",
                "expires_at": "2020-01-01T00:00:00Z",
            },
        )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_notification_publish_rejects_conflicting_idempotency_sources(api_app):
    app, _ = api_app
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/notifications/events",
            headers={"Idempotency-Key": "header-key"},
            json={
                "event_type": "risk.halted",
                "severity": "critical",
                "source": "risk",
                "title": "halt",
                "body": "stopped",
                "idempotency_key": "body-key",
            },
        )
    assert response.status_code == 422


def test_endpoint_write_rejects_credential_bearing_webhook_url():
    with pytest.raises(ValueError, match="credentials"):
        from trading_platform.ledger.api.notifications import EndpointWrite

        EndpointWrite(
            connector_id=uuid4(),
            name="unsafe",
            address="https://user:password@example.com/hook",
        )
