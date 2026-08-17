"""Notification repository integration tests against an isolated PostgreSQL schema."""

import os
import urllib.parse
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from trading_platform.ledger.db.migrations import apply_migrations
from trading_platform.ledger.db.models import create_connection_pool
from trading_platform.notifications.domain import ConnectorType, Severity
from trading_platform.notifications.repository import (
    NotificationConflictError,
    NotificationRepository,
)


pytestmark = pytest.mark.skipif(
    not os.getenv("LEDGER_TEST_DSN"), reason="LEDGER_TEST_DSN not set"
)


@pytest.fixture
async def notification_repository():
    base_dsn = os.environ["LEDGER_TEST_DSN"]
    admin_pool = await create_connection_pool(base_dsn, 1, 2)
    schema = f"notification_{uuid4().hex}"
    async with admin_pool.connection() as conn:
        await conn.execute(f'CREATE SCHEMA "{schema}"')
    await admin_pool.close()
    dsn = base_dsn + "?options=" + urllib.parse.quote(f"-csearch_path={schema}")
    pool = await create_connection_pool(dsn, 1, 4)
    await apply_migrations(pool, schema=schema)
    try:
        yield NotificationRepository(pool)
    finally:
        await pool.close()
        cleanup = await create_connection_pool(base_dsn, 1, 2)
        async with cleanup.connection() as conn:
            await conn.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await cleanup.close()


@pytest.mark.asyncio
async def test_publish_routes_exact_before_higher_priority_glob(notification_repository):
    repo = notification_repository
    connector = await repo.create_connector(
        name="ops", type=ConnectorType.WEBHOOK, secret_ref="env:OPS",
        config={}, enabled=True,
    )
    first = await repo.create_endpoint(
        connector_id=connector.id, name="first", address="https://one.invalid",
        config={}, enabled=True,
    )
    second = await repo.create_endpoint(
        connector_id=connector.id, name="second", address="https://two.invalid",
        config={}, enabled=True,
    )
    exact_group = await repo.create_group(
        name="exact", description=None, enabled=True, endpoint_ids=[first.id]
    )
    glob_group = await repo.create_group(
        name="glob", description=None, enabled=True, endpoint_ids=[second.id]
    )
    await repo.create_policy(
        name="glob-high", event_pattern="risk.*", severity=Severity.CRITICAL,
        priority=100, suppress=False, enabled=True, group_ids=[glob_group.id]
    )
    await repo.create_policy(
        name="exact-low", event_pattern="risk.halted", severity=Severity.CRITICAL,
        priority=1, suppress=False, enabled=True, group_ids=[exact_group.id]
    )
    result = await repo.publish_event(
        event_type="risk.halted", severity=Severity.CRITICAL, source="risk",
        title="halt", body="x", payload={}, idempotency_key="halt-1",
    )
    assert result.event.routing_status.value == "routed"
    assert [item.endpoint_id for item in result.deliveries] == [first.id]


@pytest.mark.asyncio
async def test_one_webhook_connector_routes_to_each_endpoint(notification_repository):
    repo = notification_repository
    connector = await repo.create_connector(
        name="ops-webhooks",
        type=ConnectorType.WEBHOOK,
        secret_ref=None,
        config={"auth_type": "none"},
        enabled=True,
    )
    primary = await repo.create_endpoint(
        connector_id=connector.id,
        name="primary",
        address="https://primary.invalid/notify",
        config={},
        enabled=True,
    )
    backup = await repo.create_endpoint(
        connector_id=connector.id,
        name="backup",
        address="https://backup.invalid/notify",
        config={},
        enabled=True,
    )
    group = await repo.create_group(
        name="incident-webhooks",
        description=None,
        enabled=True,
        endpoint_ids=[primary.id, backup.id],
    )
    await repo.create_policy(
        name="system-critical",
        event_pattern="system.*",
        severity=Severity.CRITICAL,
        priority=10,
        suppress=False,
        enabled=True,
        group_ids=[group.id],
    )

    result = await repo.publish_event(
        event_type="system.database.down",
        severity=Severity.CRITICAL,
        source="database",
        title="database unavailable",
        body="connection checks failed",
        payload={},
        idempotency_key="database-down-1",
    )

    assert {item.endpoint_id for item in result.deliveries} == {
        primary.id,
        backup.id,
    }
    assert {
        item.connector_snapshot["id"] for item in result.deliveries
    } == {str(connector.id)}


@pytest.mark.asyncio
async def test_telegram_connectors_route_by_responsibility(notification_repository):
    repo = notification_repository
    risk_bot = await repo.create_connector(
        name="risk-bot",
        type=ConnectorType.TELEGRAM,
        secret_ref="env:TG_RISK_BOT_TOKEN",
        config={},
        enabled=True,
    )
    signal_bot = await repo.create_connector(
        name="signal-bot",
        type=ConnectorType.TELEGRAM,
        secret_ref="env:TG_SIGNAL_BOT_TOKEN",
        config={},
        enabled=True,
    )
    risk_chat = await repo.create_endpoint(
        connector_id=risk_bot.id,
        name="risk-room",
        address="-1001",
        config={},
        enabled=True,
    )
    signal_chat = await repo.create_endpoint(
        connector_id=signal_bot.id,
        name="signal-room",
        address="-1002",
        config={"message_thread_id": 7},
        enabled=True,
    )
    risk_group = await repo.create_group(
        name="risk-oncall",
        description=None,
        enabled=True,
        endpoint_ids=[risk_chat.id],
    )
    signal_group = await repo.create_group(
        name="signal-watchers",
        description=None,
        enabled=True,
        endpoint_ids=[signal_chat.id],
    )
    await repo.create_policy(
        name="risk-owner",
        event_pattern="risk.*",
        severity=Severity.WARNING,
        priority=10,
        suppress=False,
        enabled=True,
        group_ids=[risk_group.id],
    )
    await repo.create_policy(
        name="signal-owner",
        event_pattern="trading.signal.*",
        severity=Severity.WARNING,
        priority=10,
        suppress=False,
        enabled=True,
        group_ids=[signal_group.id],
    )

    risk = await repo.publish_event(
        event_type="risk.exposure.high",
        severity=Severity.WARNING,
        source="risk",
        title="exposure warning",
        body="position exposure exceeded the warning threshold",
        payload={},
        idempotency_key="risk-1",
    )
    signal = await repo.publish_event(
        event_type="trading.signal.triggered",
        severity=Severity.WARNING,
        source="strategy.spike",
        title="signal warning",
        body="confirmed trading signal",
        payload={},
        idempotency_key="signal-1",
    )

    assert [item.endpoint_id for item in risk.deliveries] == [risk_chat.id]
    assert [item.endpoint_id for item in signal.deliveries] == [signal_chat.id]
    assert risk.deliveries[0].connector_snapshot["id"] == str(risk_bot.id)
    assert signal.deliveries[0].connector_snapshot["id"] == str(signal_bot.id)


@pytest.mark.asyncio
async def test_duplicate_endpoint_membership_is_one_delivery(notification_repository):
    repo = notification_repository
    connector = await repo.create_connector(
        name="ops", type=ConnectorType.WEBHOOK, secret_ref=None,
        config={}, enabled=True,
    )
    endpoint = await repo.create_endpoint(
        connector_id=connector.id, name="same", address="https://one.invalid",
        config={}, enabled=True,
    )
    group = await repo.create_group(
        name="ops", description=None, enabled=True, endpoint_ids=[endpoint.id]
    )
    await repo.create_policy(
        name="all", event_pattern="*", severity=Severity.WARNING, priority=1,
        suppress=False, enabled=True, group_ids=[group.id]
    )
    first = await repo.publish_event(
        event_type="market.degraded", severity=Severity.WARNING, source="market",
        title="degraded", body="x", payload={}, idempotency_key="m-1",
    )
    second = await repo.publish_event(
        event_type="market.degraded", severity=Severity.WARNING, source="market",
        title="degraded", body="x", payload={}, idempotency_key="m-1",
    )
    assert first.created is True
    assert second.created is False
    assert len(first.deliveries) == len(second.deliveries) == 1
    with pytest.raises(NotificationConflictError, match="idempotency key"):
        await repo.publish_event(
            event_type="market.degraded", severity=Severity.WARNING, source="market",
            title="different", body="x", payload={}, idempotency_key="m-1",
        )


@pytest.mark.asyncio
async def test_claim_lease_and_manual_retry(notification_repository):
    repo = notification_repository
    connector = await repo.create_connector(
        name="ops", type=ConnectorType.WEBHOOK, secret_ref=None,
        config={}, enabled=True,
    )
    endpoint = await repo.create_endpoint(
        connector_id=connector.id, name="ops", address="https://one.invalid",
        config={}, enabled=True,
    )
    group = await repo.create_group(
        name="ops", description=None, enabled=True, endpoint_ids=[endpoint.id]
    )
    await repo.create_policy(
        name="warn", event_pattern="warning", severity=Severity.WARNING, priority=1,
        suppress=False, enabled=True, group_ids=[group.id]
    )
    result = await repo.publish_event(
        event_type="warning", severity=Severity.WARNING, source="svc",
        title="warning", body="x", payload={}, idempotency_key="w-1",
        expires_at=datetime.now(UTC) + timedelta(minutes=1),
    )
    claim = (await repo.claim_deliveries("worker", limit=1, lease_seconds=60))[0]
    assert claim.delivery.attempt_count == 1
    # The worker's established positional worker_id/error call remains supported.
    assert await repo.mark_delivery_dead(claim.delivery.id, "worker", error="bad")
    retried = await repo.retry_delivery(claim.delivery.id)
    assert retried is not None and retried.status.value == "pending"
    assert retried.attempt_count == 0
