from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute

from trading_platform.ledger.api.notifications import (
    ConnectorUpdate,
    get_repository,
    require_same_origin,
    router,
)
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
            version=1, created_at=now, updated_at=now, has_secret=True,
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
        self.create_connector_calls = []
        self.update_connector_calls = []
        self.groups = [self.group]

    async def create_connector(self, **kwargs):
        self.create_connector_calls.append(kwargs)
        return self.connector

    async def update_connector(self, _id, **kwargs):
        self.update_connector_calls.append((_id, kwargs))
        return self.connector

    async def get_connector(self, _id):
        return self.connector if _id == self.connector.id else None

    async def list_connectors(self, *, limit, offset):
        return [self.connector], 1

    async def list_groups(self, *, limit, offset):
        return self.groups, len(self.groups)

    async def publish_event(self, **kwargs):
        from trading_platform.notifications.domain import PublishResult
        return PublishResult(self.event, (self.delivery,), True)

    async def overview(self):
        return {
            "connectors": 1,
            "enabled_connectors": 1,
            "endpoints": 1,
            "enabled_endpoints": 1,
            "groups": 1,
            "policies": 1,
            "routable_policies": 1,
            "critical_routes_ready": True,
            "critical_routes": {
                "risk.halted": True,
                "system.strategy.unhealthy": True,
            },
            "events": 1,
            "recent_events": 1,
            "unrouted_events": 0,
            "deliveries": {"pending": 1},
        }


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
    assert listed.json()["items"][0]["has_secret"] is True
    assert "secret_ref" not in listed.json()["items"][0]
    assert created.status_code == 201
    assert published.status_code == 202
    assert published.json()["event"]["routing_status"] == "routed"
    assert len(published.json()["deliveries"]) == 1
    assert "secret_ref" not in published.json()["deliveries"][0]["connector_snapshot"]


@pytest.mark.asyncio
async def test_connector_secret_is_write_only_and_blank_update_preserves_ref(api_app):
    app, repository = api_app
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/api/v1/notifications/connectors",
            json={
                "name": "direct-secret",
                "type": "telegram",
                "secret": "123:direct-secret",
                "config": {},
            },
        )
        updated = await client.put(
            f"/api/v1/notifications/connectors/{repository.connector.id}",
            json={
                "name": repository.connector.name,
                "type": "telegram",
                "config": repository.connector.config,
                "enabled": True,
                "expected_version": 1,
            },
        )

    assert created.status_code == 201
    assert repository.create_connector_calls[-1]["secret"] == "123:direct-secret"
    assert "secret" not in created.json()
    assert updated.status_code == 200
    update_values = repository.update_connector_calls[-1][1]
    assert update_values["secret"] is None
    assert update_values["secret_ref"] == "env:TG_OPS"
    assert "secret" not in updated.json()


@pytest.mark.asyncio
async def test_connector_update_rejects_both_secret_and_secret_ref(api_app):
    app, repository = api_app
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.put(
            f"/api/v1/notifications/connectors/{repository.connector.id}",
            json={
                "name": repository.connector.name,
                "type": "telegram",
                "secret": "123:new-secret",
                "secret_ref": "env:TG_OPS",
                "config": repository.connector.config,
                "enabled": True,
                "expected_version": 1,
            },
        )

    assert response.status_code == 422
    assert repository.update_connector_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("origin", "status_code"),
    [
        (None, 201),
        ("http://test", 201),
        ("http://test:80", 201),
        ("http://other", 403),
        ("http://test:81", 403),
        ("https://test", 403),
        ("null", 403),
    ],
)
async def test_notification_writes_enforce_same_origin(api_app, origin, status_code):
    app, _ = api_app
    transport = httpx.ASGITransport(app=app)
    headers = {} if origin is None else {"Origin": origin}
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/notifications/connectors",
            headers=headers,
            json={
                "name": "origin-check",
                "type": "telegram",
                "secret_ref": "env:TG_OPS",
                "config": {},
            },
        )
    assert response.status_code == status_code


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["secret", "secret_ref"])
async def test_connector_update_rejects_clear_secret_with_credential(api_app, field):
    app, repository = api_app
    transport = httpx.ASGITransport(app=app)
    body = {
        "name": repository.connector.name,
        "type": "telegram",
        "config": repository.connector.config,
        "enabled": True,
        "expected_version": 1,
        "clear_secret": True,
        field: "123:new-secret" if field == "secret" else "env:NEW_SECRET",
    }
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.put(
            f"/api/v1/notifications/connectors/{repository.connector.id}",
            json=body,
        )
    assert response.status_code == 422
    assert repository.update_connector_calls == []


def test_all_notification_write_routes_have_same_origin_dependency():
    write_paths = {
        "/api/v1/notifications/connectors",
        "/api/v1/notifications/connectors/{connector_id}",
        "/api/v1/notifications/endpoints",
        "/api/v1/notifications/endpoints/{endpoint_id}",
        "/api/v1/notifications/endpoints/{endpoint_id}/test",
        "/api/v1/notifications/groups",
        "/api/v1/notifications/groups/{group_id}",
        "/api/v1/notifications/policies",
        "/api/v1/notifications/policies/{policy_id}",
        "/api/v1/notifications/events",
        "/api/v1/notifications/deliveries/{delivery_id}/retry",
    }
    routes = {
        route.path: route
        for route in router.routes
        if isinstance(route, APIRoute)
        and route.path in write_paths
        and route.methods & {"POST", "PUT", "DELETE"}
    }
    assert set(routes) == write_paths
    for route in routes.values():
        assert any(
            dependency.call is require_same_origin
            for dependency in route.dependant.dependencies
        )


@pytest.mark.asyncio
async def test_group_response_keeps_endpoint_ids_without_endpoint_details(api_app):
    app, _ = api_app
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/notifications/groups")

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["endpoint_ids"] == [str(api_app[1].endpoint.id)]
    assert "endpoint_details" not in item


@pytest.mark.asyncio
async def test_notification_overview_exposes_routable_policy_count(api_app):
    app, _ = api_app
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/notifications/overview")

    assert response.status_code == 200
    assert response.json()["routable_policies"] == 1
    assert response.json()["critical_routes_ready"] is True
    assert response.json()["critical_routes"] == {
        "risk.halted": True,
        "system.strategy.unhealthy": True,
    }
    assert response.json()["recent_events"] == 1


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


def test_connector_update_allows_blank_secret_for_preserving_existing_credential():
    update = ConnectorUpdate(
        name="ops",
        type="telegram",
        secret_ref=None,
        secret=None,
        config={},
        enabled=True,
        expected_version=1,
    )
    assert update.secret is None
    assert update.secret_ref is None


@pytest.mark.parametrize(
    "secret_ref",
    ["123456:ABCDEF", "file:/tmp/token", "env:1TOKEN", "unsafe/name", "safe:name"],
)
def test_connector_secret_ref_rejects_unsafe_reference(secret_ref):
    with pytest.raises(ValueError, match="secret_ref"):
        ConnectorUpdate(
            name="ops",
            type="telegram",
            secret_ref=secret_ref,
            config={},
            enabled=True,
            expected_version=1,
        )


def test_connector_update_rejects_both_secret_and_secret_ref():
    with pytest.raises(ValueError, match="either secret or secret_ref"):
        ConnectorUpdate(
            name="ops",
            type="telegram",
            secret="123:new-secret",
            secret_ref="env:TG_OPS",
            config={},
            enabled=True,
            expected_version=1,
        )
