"""PostgreSQL persistence for notification configuration and delivery state."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Iterable
from uuid import UUID, uuid4

from psycopg.errors import ForeignKeyViolation, UniqueViolation
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from trading_platform.notifications.domain import (
    ConnectorType,
    DeliveryClaim,
    DeliveryStatus,
    NotificationConnector,
    NotificationDelivery,
    NotificationEndpoint,
    NotificationEvent,
    NotificationGroup,
    NotificationPolicy,
    PublishResult,
    RoutingStatus,
    Severity,
    choose_policy,
)


class NotificationConflictError(RuntimeError):
    """A unique name or idempotency identity conflicts with existing state."""


class NotificationVersionConflictError(RuntimeError):
    """An optimistic configuration update used an obsolete version."""


class NotificationReferenceError(RuntimeError):
    """A referenced connector, endpoint, or group does not exist."""


class NotificationResourceInUseError(RuntimeError):
    """A configuration object is still referenced by another object."""


class NotificationStateError(RuntimeError):
    """The requested state transition is not allowed."""


def _connector(row: dict[str, Any]) -> NotificationConnector:
    return NotificationConnector(
        **{**row, "type": ConnectorType(row["type"])}
    )


def _endpoint(row: dict[str, Any]) -> NotificationEndpoint:
    return NotificationEndpoint(**row)


def _group(row: dict[str, Any]) -> NotificationGroup:
    return NotificationGroup(
        **{**row, "endpoint_ids": tuple(row.get("endpoint_ids") or ())}
    )


def _policy(row: dict[str, Any]) -> NotificationPolicy:
    return NotificationPolicy(
        **{
            **row,
            "severity": Severity(row["severity"]),
            "group_ids": tuple(row.get("group_ids") or ()),
        }
    )


def _event(row: dict[str, Any]) -> NotificationEvent:
    return NotificationEvent(
        **{
            **row,
            "severity": Severity(row["severity"]),
            "routing_status": RoutingStatus(row["routing_status"]),
        }
    )


def _delivery(row: dict[str, Any]) -> NotificationDelivery:
    return NotificationDelivery(
        **{**row, "status": DeliveryStatus(row["status"])}
    )


class NotificationRepository:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self.pool = pool

    @staticmethod
    async def _fetchone(
        conn: object, query: str, parameters: object = ()
    ) -> dict[str, Any] | None:
        cursor = conn.cursor(row_factory=dict_row)
        await cursor.execute(query, parameters)
        return await cursor.fetchone()

    @staticmethod
    async def _fetchall(
        conn: object, query: str, parameters: object = ()
    ) -> list[dict[str, Any]]:
        cursor = conn.cursor(row_factory=dict_row)
        await cursor.execute(query, parameters)
        return list(await cursor.fetchall())

    async def _read_one(
        self, query: str, parameters: object = ()
    ) -> dict[str, Any] | None:
        async with self.pool.connection() as conn:
            return await self._fetchone(conn, query, parameters)

    async def _read_many(
        self, query: str, parameters: object = ()
    ) -> list[dict[str, Any]]:
        async with self.pool.connection() as conn:
            return await self._fetchall(conn, query, parameters)

    @staticmethod
    async def _ensure_ids(
        conn: object,
        table: str,
        ids: Iterable[UUID],
        *,
        label: str,
    ) -> tuple[UUID, ...]:
        unique_ids = tuple(dict.fromkeys(ids))
        if not unique_ids:
            return unique_ids
        rows = await NotificationRepository._fetchall(
            conn,
            f"SELECT id FROM {table} WHERE id = ANY(%s)",
            (list(unique_ids),),
        )
        found = {row["id"] for row in rows}
        if found != set(unique_ids):
            raise NotificationReferenceError(f"unknown {label}")
        return unique_ids

    @staticmethod
    async def _raise_missing_or_version(
        conn: object, table: str, object_id: UUID
    ) -> None:
        row = await NotificationRepository._fetchone(
            conn, f"SELECT 1 AS present FROM {table} WHERE id = %s", (object_id,)
        )
        if row is not None:
            raise NotificationVersionConflictError("configuration version conflict")

    async def create_connector(
        self,
        *,
        name: str,
        type: ConnectorType,
        secret_ref: str | None,
        config: dict[str, Any],
        enabled: bool,
    ) -> NotificationConnector:
        connector_id = uuid4()
        try:
            async with self.pool.connection() as conn:
                row = await self._fetchone(
                    conn,
                    "INSERT INTO notification_connectors "
                    "(id, name, type, secret_ref, config, enabled) "
                    "VALUES (%s, %s, %s, %s, %s, %s) RETURNING *",
                    (
                        connector_id,
                        name,
                        type.value,
                        secret_ref,
                        Jsonb(config),
                        enabled,
                    ),
                )
        except UniqueViolation as error:
            raise NotificationConflictError("connector name already exists") from error
        assert row is not None
        return _connector(row)

    async def list_connectors(
        self, *, limit: int, offset: int
    ) -> tuple[list[NotificationConnector], int]:
        async with self.pool.connection() as conn:
            rows = await self._fetchall(
                conn,
                "SELECT * FROM notification_connectors "
                "ORDER BY name, id LIMIT %s OFFSET %s",
                (limit, offset),
            )
            total = await self._fetchone(
                conn, "SELECT COUNT(*) AS count FROM notification_connectors"
            )
        return [_connector(row) for row in rows], int(total["count"])

    async def get_connector(
        self, connector_id: UUID
    ) -> NotificationConnector | None:
        row = await self._read_one(
            "SELECT * FROM notification_connectors WHERE id = %s", (connector_id,)
        )
        return _connector(row) if row is not None else None

    async def update_connector(
        self,
        connector_id: UUID,
        *,
        expected_version: int,
        name: str,
        type: ConnectorType,
        secret_ref: str | None,
        config: dict[str, Any],
        enabled: bool,
    ) -> NotificationConnector | None:
        try:
            async with self.pool.connection() as conn:
                row = await self._fetchone(
                    conn,
                    "UPDATE notification_connectors SET name = %s, type = %s, "
                    "secret_ref = %s, config = %s, enabled = %s, "
                    "version = version + 1, updated_at = NOW() "
                    "WHERE id = %s AND version = %s RETURNING *",
                    (
                        name,
                        type.value,
                        secret_ref,
                        Jsonb(config),
                        enabled,
                        connector_id,
                        expected_version,
                    ),
                )
                if row is None:
                    await self._raise_missing_or_version(
                        conn, "notification_connectors", connector_id
                    )
                    return None
        except UniqueViolation as error:
            raise NotificationConflictError("connector name already exists") from error
        return _connector(row)

    async def delete_connector(
        self, connector_id: UUID, *, expected_version: int
    ) -> bool:
        try:
            async with self.pool.connection() as conn:
                result = await conn.execute(
                    "DELETE FROM notification_connectors "
                    "WHERE id = %s AND version = %s",
                    (connector_id, expected_version),
                )
                if result.rowcount == 0:
                    await self._raise_missing_or_version(
                        conn, "notification_connectors", connector_id
                    )
                    return False
        except ForeignKeyViolation as error:
            raise NotificationResourceInUseError(
                "connector still has endpoints"
            ) from error
        return True

    async def create_endpoint(
        self,
        *,
        connector_id: UUID,
        name: str,
        address: str,
        config: dict[str, Any],
        enabled: bool,
    ) -> NotificationEndpoint:
        endpoint_id = uuid4()
        try:
            async with self.pool.connection() as conn:
                row = await self._fetchone(
                    conn,
                    "INSERT INTO notification_endpoints "
                    "(id, connector_id, name, address, config, enabled) "
                    "VALUES (%s, %s, %s, %s, %s, %s) RETURNING *",
                    (
                        endpoint_id,
                        connector_id,
                        name,
                        address,
                        Jsonb(config),
                        enabled,
                    ),
                )
        except UniqueViolation as error:
            raise NotificationConflictError(
                "endpoint name already exists for connector"
            ) from error
        except ForeignKeyViolation as error:
            raise NotificationReferenceError("unknown connector") from error
        assert row is not None
        return _endpoint(row)

    async def list_endpoints(
        self,
        *,
        connector_id: UUID | None,
        limit: int,
        offset: int,
    ) -> tuple[list[NotificationEndpoint], int]:
        where = " WHERE connector_id = %s" if connector_id else ""
        params: tuple[object, ...] = (connector_id,) if connector_id else ()
        async with self.pool.connection() as conn:
            rows = await self._fetchall(
                conn,
                "SELECT * FROM notification_endpoints"
                f"{where} ORDER BY name, id LIMIT %s OFFSET %s",
                (*params, limit, offset),
            )
            total = await self._fetchone(
                conn,
                f"SELECT COUNT(*) AS count FROM notification_endpoints{where}",
                params,
            )
        return [_endpoint(row) for row in rows], int(total["count"])

    async def get_endpoint(
        self, endpoint_id: UUID
    ) -> NotificationEndpoint | None:
        row = await self._read_one(
            "SELECT * FROM notification_endpoints WHERE id = %s", (endpoint_id,)
        )
        return _endpoint(row) if row is not None else None

    async def update_endpoint(
        self,
        endpoint_id: UUID,
        *,
        expected_version: int,
        connector_id: UUID,
        name: str,
        address: str,
        config: dict[str, Any],
        enabled: bool,
    ) -> NotificationEndpoint | None:
        try:
            async with self.pool.connection() as conn:
                row = await self._fetchone(
                    conn,
                    "UPDATE notification_endpoints SET connector_id = %s, "
                    "name = %s, address = %s, config = %s, enabled = %s, "
                    "version = version + 1, updated_at = NOW() "
                    "WHERE id = %s AND version = %s RETURNING *",
                    (
                        connector_id,
                        name,
                        address,
                        Jsonb(config),
                        enabled,
                        endpoint_id,
                        expected_version,
                    ),
                )
                if row is None:
                    await self._raise_missing_or_version(
                        conn, "notification_endpoints", endpoint_id
                    )
                    return None
        except UniqueViolation as error:
            raise NotificationConflictError(
                "endpoint name already exists for connector"
            ) from error
        except ForeignKeyViolation as error:
            raise NotificationReferenceError("unknown connector") from error
        return _endpoint(row)

    async def delete_endpoint(
        self, endpoint_id: UUID, *, expected_version: int
    ) -> bool:
        try:
            async with self.pool.connection() as conn:
                result = await conn.execute(
                    "DELETE FROM notification_endpoints "
                    "WHERE id = %s AND version = %s",
                    (endpoint_id, expected_version),
                )
                if result.rowcount == 0:
                    await self._raise_missing_or_version(
                        conn, "notification_endpoints", endpoint_id
                    )
                    return False
        except ForeignKeyViolation as error:
            raise NotificationResourceInUseError(
                "endpoint is used by a group or delivery"
            ) from error
        return True

    @staticmethod
    def _group_select(where: str = "") -> str:
        return (
            "SELECT g.*, COALESCE(array_agg(m.endpoint_id ORDER BY m.endpoint_id) "
            "FILTER (WHERE m.endpoint_id IS NOT NULL), '{}'::UUID[]) AS endpoint_ids "
            "FROM notification_groups g LEFT JOIN notification_group_members m "
            "ON m.group_id = g.id " + where + " GROUP BY g.id"
        )

    async def create_group(
        self,
        *,
        name: str,
        description: str | None,
        enabled: bool,
        endpoint_ids: Iterable[UUID],
    ) -> NotificationGroup:
        group_id = uuid4()
        try:
            async with self.pool.connection() as conn:
                async with conn.transaction():
                    member_ids = await self._ensure_ids(
                        conn,
                        "notification_endpoints",
                        endpoint_ids,
                        label="endpoint",
                    )
                    await conn.execute(
                        "INSERT INTO notification_groups "
                        "(id, name, description, enabled) VALUES (%s, %s, %s, %s)",
                        (group_id, name, description, enabled),
                    )
                    for endpoint_id in member_ids:
                        await conn.execute(
                            "INSERT INTO notification_group_members "
                            "(group_id, endpoint_id) VALUES (%s, %s)",
                            (group_id, endpoint_id),
                        )
                    row = await self._fetchone(
                        conn, self._group_select("WHERE g.id = %s"), (group_id,)
                    )
        except UniqueViolation as error:
            raise NotificationConflictError("group name already exists") from error
        assert row is not None
        return _group(row)

    async def list_groups(
        self, *, limit: int, offset: int
    ) -> tuple[list[NotificationGroup], int]:
        async with self.pool.connection() as conn:
            rows = await self._fetchall(
                conn,
                self._group_select() + " ORDER BY g.name, g.id LIMIT %s OFFSET %s",
                (limit, offset),
            )
            total = await self._fetchone(
                conn, "SELECT COUNT(*) AS count FROM notification_groups"
            )
        return [_group(row) for row in rows], int(total["count"])

    async def get_group(self, group_id: UUID) -> NotificationGroup | None:
        row = await self._read_one(
            self._group_select("WHERE g.id = %s"), (group_id,)
        )
        return _group(row) if row is not None else None

    async def update_group(
        self,
        group_id: UUID,
        *,
        expected_version: int,
        name: str,
        description: str | None,
        enabled: bool,
        endpoint_ids: Iterable[UUID],
    ) -> NotificationGroup | None:
        try:
            async with self.pool.connection() as conn:
                async with conn.transaction():
                    member_ids = await self._ensure_ids(
                        conn,
                        "notification_endpoints",
                        endpoint_ids,
                        label="endpoint",
                    )
                    updated = await self._fetchone(
                        conn,
                        "UPDATE notification_groups SET name = %s, description = %s, "
                        "enabled = %s, version = version + 1, updated_at = NOW() "
                        "WHERE id = %s AND version = %s RETURNING id",
                        (name, description, enabled, group_id, expected_version),
                    )
                    if updated is None:
                        await self._raise_missing_or_version(
                            conn, "notification_groups", group_id
                        )
                        return None
                    await conn.execute(
                        "DELETE FROM notification_group_members WHERE group_id = %s",
                        (group_id,),
                    )
                    for endpoint_id in member_ids:
                        await conn.execute(
                            "INSERT INTO notification_group_members "
                            "(group_id, endpoint_id) VALUES (%s, %s)",
                            (group_id, endpoint_id),
                        )
                    row = await self._fetchone(
                        conn, self._group_select("WHERE g.id = %s"), (group_id,)
                    )
        except UniqueViolation as error:
            raise NotificationConflictError("group name already exists") from error
        assert row is not None
        return _group(row)

    async def delete_group(
        self, group_id: UUID, *, expected_version: int
    ) -> bool:
        try:
            async with self.pool.connection() as conn:
                result = await conn.execute(
                    "DELETE FROM notification_groups WHERE id = %s AND version = %s",
                    (group_id, expected_version),
                )
                if result.rowcount == 0:
                    await self._raise_missing_or_version(
                        conn, "notification_groups", group_id
                    )
                    return False
        except ForeignKeyViolation as error:
            raise NotificationResourceInUseError(
                "group is used by a policy"
            ) from error
        return True

    @staticmethod
    def _policy_select(where: str = "") -> str:
        return (
            "SELECT p.*, COALESCE(array_agg(pg.group_id ORDER BY pg.group_id) "
            "FILTER (WHERE pg.group_id IS NOT NULL), '{}'::UUID[]) AS group_ids "
            "FROM notification_policies p LEFT JOIN notification_policy_groups pg "
            "ON pg.policy_id = p.id " + where + " GROUP BY p.id"
        )

    async def create_policy(
        self,
        *,
        name: str,
        event_pattern: str,
        severity: Severity,
        priority: int,
        suppress: bool,
        enabled: bool,
        group_ids: Iterable[UUID],
    ) -> NotificationPolicy:
        policy_id = uuid4()
        try:
            async with self.pool.connection() as conn:
                async with conn.transaction():
                    target_ids = await self._ensure_ids(
                        conn,
                        "notification_groups",
                        group_ids,
                        label="group",
                    )
                    await conn.execute(
                        "INSERT INTO notification_policies "
                        "(id, name, event_pattern, severity, priority, suppress, enabled) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                        (
                            policy_id,
                            name,
                            event_pattern,
                            severity.value,
                            priority,
                            suppress,
                            enabled,
                        ),
                    )
                    for group_id in target_ids:
                        await conn.execute(
                            "INSERT INTO notification_policy_groups "
                            "(policy_id, group_id) VALUES (%s, %s)",
                            (policy_id, group_id),
                        )
                    row = await self._fetchone(
                        conn, self._policy_select("WHERE p.id = %s"), (policy_id,)
                    )
        except UniqueViolation as error:
            raise NotificationConflictError("policy name already exists") from error
        assert row is not None
        return _policy(row)

    async def list_policies(
        self, *, limit: int, offset: int
    ) -> tuple[list[NotificationPolicy], int]:
        async with self.pool.connection() as conn:
            rows = await self._fetchall(
                conn,
                self._policy_select()
                + " ORDER BY p.priority DESC, p.name, p.id LIMIT %s OFFSET %s",
                (limit, offset),
            )
            total = await self._fetchone(
                conn, "SELECT COUNT(*) AS count FROM notification_policies"
            )
        return [_policy(row) for row in rows], int(total["count"])

    async def get_policy(self, policy_id: UUID) -> NotificationPolicy | None:
        row = await self._read_one(
            self._policy_select("WHERE p.id = %s"), (policy_id,)
        )
        return _policy(row) if row is not None else None

    async def update_policy(
        self,
        policy_id: UUID,
        *,
        expected_version: int,
        name: str,
        event_pattern: str,
        severity: Severity,
        priority: int,
        suppress: bool,
        enabled: bool,
        group_ids: Iterable[UUID],
    ) -> NotificationPolicy | None:
        try:
            async with self.pool.connection() as conn:
                async with conn.transaction():
                    target_ids = await self._ensure_ids(
                        conn,
                        "notification_groups",
                        group_ids,
                        label="group",
                    )
                    updated = await self._fetchone(
                        conn,
                        "UPDATE notification_policies SET name = %s, "
                        "event_pattern = %s, severity = %s, priority = %s, "
                        "suppress = %s, enabled = %s, version = version + 1, "
                        "updated_at = NOW() WHERE id = %s AND version = %s "
                        "RETURNING id",
                        (
                            name,
                            event_pattern,
                            severity.value,
                            priority,
                            suppress,
                            enabled,
                            policy_id,
                            expected_version,
                        ),
                    )
                    if updated is None:
                        await self._raise_missing_or_version(
                            conn, "notification_policies", policy_id
                        )
                        return None
                    await conn.execute(
                        "DELETE FROM notification_policy_groups WHERE policy_id = %s",
                        (policy_id,),
                    )
                    for group_id in target_ids:
                        await conn.execute(
                            "INSERT INTO notification_policy_groups "
                            "(policy_id, group_id) VALUES (%s, %s)",
                            (policy_id, group_id),
                        )
                    row = await self._fetchone(
                        conn, self._policy_select("WHERE p.id = %s"), (policy_id,)
                    )
        except UniqueViolation as error:
            raise NotificationConflictError("policy name already exists") from error
        assert row is not None
        return _policy(row)

    async def delete_policy(
        self, policy_id: UUID, *, expected_version: int
    ) -> bool:
        async with self.pool.connection() as conn:
            result = await conn.execute(
                "DELETE FROM notification_policies WHERE id = %s AND version = %s",
                (policy_id, expected_version),
            )
            if result.rowcount == 0:
                await self._raise_missing_or_version(
                    conn, "notification_policies", policy_id
                )
                return False
        return True

    async def _routing_policies(
        self, conn: object, severity: Severity
    ) -> list[NotificationPolicy]:
        rows = await self._fetchall(
            conn,
            self._policy_select("WHERE p.enabled = TRUE AND p.severity = %s")
            + " ORDER BY p.created_at, p.id",
            (severity.value,),
        )
        return [_policy(row) for row in rows]

    async def _policy_endpoints(
        self, conn: object, policy_id: UUID
    ) -> list[dict[str, Any]]:
        return await self._fetchall(
            conn,
            "SELECT DISTINCT e.id AS endpoint_id, e.name AS endpoint_name, "
            "e.address, e.config AS endpoint_config, e.version AS endpoint_version, "
            "c.id AS connector_id, c.name AS connector_name, c.type AS connector_type, "
            "c.secret_ref, c.config AS connector_config, "
            "c.version AS connector_version "
            "FROM notification_policy_groups pg "
            "JOIN notification_groups g ON g.id = pg.group_id AND g.enabled = TRUE "
            "JOIN notification_group_members gm ON gm.group_id = g.id "
            "JOIN notification_endpoints e "
            "ON e.id = gm.endpoint_id AND e.enabled = TRUE "
            "JOIN notification_connectors c "
            "ON c.id = e.connector_id AND c.enabled = TRUE "
            "WHERE pg.policy_id = %s ORDER BY e.id",
            (policy_id,),
        )

    @staticmethod
    def _snapshots(row: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        connector = {
            "id": str(row["connector_id"]),
            "name": row["connector_name"],
            "type": row["connector_type"],
            "secret_ref": row["secret_ref"],
            "config": row["connector_config"],
            "version": row["connector_version"],
        }
        endpoint = {
            "id": str(row["endpoint_id"]),
            "name": row["endpoint_name"],
            "address": row["address"],
            "config": row["endpoint_config"],
            "version": row["endpoint_version"],
        }
        return connector, endpoint

    async def _insert_delivery(
        self,
        conn: object,
        *,
        event_id: UUID,
        endpoint_row: dict[str, Any],
    ) -> None:
        connector, endpoint = self._snapshots(endpoint_row)
        await conn.execute(
            "INSERT INTO notification_deliveries "
            "(id, event_id, endpoint_id, connector_snapshot, endpoint_snapshot) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (event_id, endpoint_id) DO NOTHING",
            (
                uuid4(),
                event_id,
                endpoint_row["endpoint_id"],
                Jsonb(connector),
                Jsonb(endpoint),
            ),
        )

    async def _route_event(
        self, conn: object, event: NotificationEvent
    ) -> NotificationEvent:
        selected = choose_policy(
            await self._routing_policies(conn, event.severity), event.event_type
        )
        if selected is None:
            routing_status = RoutingStatus.UNROUTED
            endpoints: list[dict[str, Any]] = []
        elif selected.suppress:
            routing_status = RoutingStatus.SUPPRESSED
            endpoints = []
        else:
            endpoints = await self._policy_endpoints(conn, selected.id)
            routing_status = (
                RoutingStatus.ROUTED if endpoints else RoutingStatus.UNROUTED
            )
            for endpoint_row in endpoints:
                await self._insert_delivery(
                    conn, event_id=event.id, endpoint_row=endpoint_row
                )
        row = await self._fetchone(
            conn,
            "UPDATE notification_events SET matched_policy_id = %s, "
            "routing_status = %s WHERE id = %s RETURNING *",
            (
                selected.id if selected is not None else None,
                routing_status.value,
                event.id,
            ),
        )
        assert row is not None
        return _event(row)

    async def _event_deliveries(
        self, conn: object, event_id: UUID
    ) -> tuple[NotificationDelivery, ...]:
        rows = await self._fetchall(
            conn,
            "SELECT * FROM notification_deliveries WHERE event_id = %s "
            "ORDER BY created_at, id",
            (event_id,),
        )
        return tuple(_delivery(row) for row in rows)

    async def publish_event(
        self,
        *,
        event_type: str,
        severity: Severity,
        source: str,
        title: str,
        body: str,
        payload: dict[str, Any],
        idempotency_key: str,
        correlation_id: str | None = None,
        fingerprint: str | None = None,
        occurred_at: datetime | None = None,
        expires_at: datetime | None = None,
    ) -> PublishResult:
        event_id = uuid4()
        occurred_at = occurred_at or datetime.now(UTC)
        async with self.pool.connection() as conn:
            async with conn.transaction():
                row = await self._fetchone(
                    conn,
                    "INSERT INTO notification_events "
                    "(id, event_type, severity, source, title, body, payload, "
                    "idempotency_key, correlation_id, fingerprint, routing_status, "
                    "occurred_at, expires_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                    "'pending', %s, %s) "
                    "ON CONFLICT (source, idempotency_key) DO NOTHING RETURNING *",
                    (
                        event_id,
                        event_type,
                        severity.value,
                        source,
                        title,
                        body,
                        Jsonb(payload),
                        idempotency_key,
                        correlation_id,
                        fingerprint,
                        occurred_at,
                        expires_at,
                    ),
                )
                if row is None:
                    existing = await self._fetchone(
                        conn,
                        "SELECT * FROM notification_events "
                        "WHERE source = %s AND idempotency_key = %s",
                        (source, idempotency_key),
                    )
                    assert existing is not None
                    existing_event = _event(existing)
                    immutable_pairs = (
                        (existing_event.event_type, event_type),
                        (existing_event.severity.value, severity.value),
                        (existing_event.source, source),
                        (existing_event.title, title),
                        (existing_event.body, body),
                        (existing_event.payload, payload),
                        (existing_event.correlation_id, correlation_id),
                        (existing_event.fingerprint, fingerprint),
                    )
                    if any(left != right for left, right in immutable_pairs):
                        raise NotificationConflictError(
                            "idempotency key is already used by a different event"
                        )
                    return PublishResult(
                        event=existing_event,
                        deliveries=await self._event_deliveries(
                            conn, existing_event.id
                        ),
                        created=False,
                    )
                routed_event = await self._route_event(conn, _event(row))
                deliveries = await self._event_deliveries(conn, routed_event.id)
        return PublishResult(routed_event, deliveries, True)

    async def create_endpoint_test(
        self,
        endpoint_id: UUID,
        *,
        title: str,
        body: str,
        payload: dict[str, Any],
    ) -> PublishResult | None:
        event_id = uuid4()
        async with self.pool.connection() as conn:
            async with conn.transaction():
                endpoint = await self._fetchone(
                    conn,
                    "SELECT e.id AS endpoint_id, e.name AS endpoint_name, "
                    "e.address, e.config AS endpoint_config, "
                    "e.version AS endpoint_version, e.enabled AS endpoint_enabled, "
                    "c.id AS connector_id, c.name AS connector_name, "
                    "c.type AS connector_type, c.secret_ref, "
                    "c.config AS connector_config, c.version AS connector_version, "
                    "c.enabled AS connector_enabled "
                    "FROM notification_endpoints e "
                    "JOIN notification_connectors c ON c.id = e.connector_id "
                    "WHERE e.id = %s FOR SHARE OF e, c",
                    (endpoint_id,),
                )
                if endpoint is None:
                    return None
                if not endpoint["endpoint_enabled"] or not endpoint["connector_enabled"]:
                    raise NotificationStateError(
                        "endpoint and connector must be enabled for a test"
                    )
                now = datetime.now(UTC)
                event_row = await self._fetchone(
                    conn,
                    "INSERT INTO notification_events "
                    "(id, event_type, severity, source, title, body, payload, "
                    "idempotency_key, routing_status, occurred_at) "
                    "VALUES (%s, 'notification.endpoint_test', 'info', "
                    "'notification.admin', %s, %s, %s, %s, 'targeted', %s) "
                    "RETURNING *",
                    (
                        event_id,
                        title,
                        body,
                        Jsonb(payload),
                        f"endpoint-test:{event_id}",
                        now,
                    ),
                )
                assert event_row is not None
                await self._insert_delivery(
                    conn, event_id=event_id, endpoint_row=endpoint
                )
                deliveries = await self._event_deliveries(conn, event_id)
        return PublishResult(_event(event_row), deliveries, True)

    async def list_events(
        self,
        *,
        event_type: str | None,
        severity: Severity | None,
        source: str | None,
        routing_status: RoutingStatus | None,
        limit: int,
        offset: int,
    ) -> tuple[list[NotificationEvent], int]:
        filters: list[str] = []
        params: list[object] = []
        for column, value in (
            ("event_type", event_type),
            ("severity", severity.value if severity else None),
            ("source", source),
            ("routing_status", routing_status.value if routing_status else None),
        ):
            if value is not None:
                filters.append(f"{column} = %s")
                params.append(value)
        where = " WHERE " + " AND ".join(filters) if filters else ""
        async with self.pool.connection() as conn:
            rows = await self._fetchall(
                conn,
                "SELECT * FROM notification_events"
                f"{where} ORDER BY created_at DESC, id DESC LIMIT %s OFFSET %s",
                (*params, limit, offset),
            )
            total = await self._fetchone(
                conn,
                f"SELECT COUNT(*) AS count FROM notification_events{where}",
                tuple(params),
            )
        return [_event(row) for row in rows], int(total["count"])

    async def get_event(self, event_id: UUID) -> NotificationEvent | None:
        row = await self._read_one(
            "SELECT * FROM notification_events WHERE id = %s", (event_id,)
        )
        return _event(row) if row is not None else None

    async def list_deliveries(
        self,
        *,
        event_id: UUID | None,
        endpoint_id: UUID | None,
        status: DeliveryStatus | None,
        limit: int,
        offset: int,
    ) -> tuple[list[NotificationDelivery], int]:
        filters: list[str] = []
        params: list[object] = []
        for column, value in (
            ("event_id", event_id),
            ("endpoint_id", endpoint_id),
            ("status", status.value if status else None),
        ):
            if value is not None:
                filters.append(f"{column} = %s")
                params.append(value)
        where = " WHERE " + " AND ".join(filters) if filters else ""
        async with self.pool.connection() as conn:
            rows = await self._fetchall(
                conn,
                "SELECT * FROM notification_deliveries"
                f"{where} ORDER BY created_at DESC, id DESC LIMIT %s OFFSET %s",
                (*params, limit, offset),
            )
            total = await self._fetchone(
                conn,
                f"SELECT COUNT(*) AS count FROM notification_deliveries{where}",
                tuple(params),
            )
        return [_delivery(row) for row in rows], int(total["count"])

    async def get_delivery(
        self, delivery_id: UUID
    ) -> NotificationDelivery | None:
        row = await self._read_one(
            "SELECT * FROM notification_deliveries WHERE id = %s", (delivery_id,)
        )
        return _delivery(row) if row is not None else None

    async def retry_delivery(
        self, delivery_id: UUID
    ) -> NotificationDelivery | None:
        async with self.pool.connection() as conn:
            async with conn.transaction():
                current = await self._fetchone(
                    conn,
                    "SELECT * FROM notification_deliveries WHERE id = %s FOR UPDATE",
                    (delivery_id,),
                )
                if current is None:
                    return None
                if current["status"] not in {"dead", "retry"}:
                    raise NotificationStateError(
                        "only dead or retry deliveries can be manually retried"
                    )
                row = await self._fetchone(
                    conn,
                    "UPDATE notification_deliveries SET status = 'pending', "
                    "attempt_count = 0, "
                    "next_attempt_at = NOW(), lease_until = NULL, lease_owner = NULL, "
                    "last_error = NULL, updated_at = NOW() WHERE id = %s RETURNING *",
                    (delivery_id,),
                )
        assert row is not None
        return _delivery(row)

    async def overview(self) -> dict[str, Any]:
        async with self.pool.connection() as conn:
            row = await self._fetchone(
                conn,
                "SELECT "
                "(SELECT COUNT(*) FROM notification_connectors) AS connectors, "
                "(SELECT COUNT(*) FROM notification_connectors WHERE enabled) "
                "AS enabled_connectors, "
                "(SELECT COUNT(*) FROM notification_endpoints) AS endpoints, "
                "(SELECT COUNT(*) FROM notification_endpoints WHERE enabled) "
                "AS enabled_endpoints, "
                "(SELECT COUNT(*) FROM notification_groups) AS groups, "
                "(SELECT COUNT(*) FROM notification_policies) AS policies, "
                "(SELECT COUNT(*) FROM notification_events) AS events, "
                "(SELECT COUNT(*) FROM notification_events "
                "WHERE created_at >= NOW() - INTERVAL '24 hours') AS recent_events, "
                "(SELECT COUNT(*) FROM notification_events "
                "WHERE routing_status = 'unrouted') AS unrouted_events"
            )
            statuses = await self._fetchall(
                conn,
                "SELECT status, COUNT(*) AS count FROM notification_deliveries "
                "GROUP BY status",
            )
        assert row is not None
        return {
            **{key: int(value) for key, value in row.items()},
            "deliveries": {item["status"]: int(item["count"]) for item in statuses},
        }

    async def route_pending_events(self, limit: int = 100) -> int:
        async with self.pool.connection() as conn:
            async with conn.transaction():
                rows = await self._fetchall(
                    conn,
                    "SELECT * FROM notification_events "
                    "WHERE routing_status = 'pending' ORDER BY created_at, id "
                    "LIMIT %s FOR UPDATE SKIP LOCKED",
                    (limit,),
                )
                for row in rows:
                    await self._route_event(conn, _event(row))
        return len(rows)

    async def claim_deliveries(
        self,
        worker_id: str,
        *,
        limit: int,
        lease_seconds: int,
    ) -> list[DeliveryClaim]:
        now = datetime.now(UTC)
        lease_until = now + timedelta(seconds=lease_seconds)
        async with self.pool.connection() as conn:
            async with conn.transaction():
                await conn.execute(
                    "UPDATE notification_deliveries SET status = 'retry', "
                    "lease_until = NULL, lease_owner = NULL, next_attempt_at = NOW(), "
                    "last_error = 'delivery lease expired', updated_at = NOW() "
                    "WHERE status = 'sending' AND lease_until <= NOW()"
                )
                await conn.execute(
                    "UPDATE notification_deliveries d SET status = 'dead', "
                    "lease_until = NULL, lease_owner = NULL, "
                    "last_error = 'event expired before delivery', updated_at = NOW() "
                    "FROM notification_events e WHERE e.id = d.event_id "
                    "AND d.status IN ('pending', 'retry') "
                    "AND e.expires_at IS NOT NULL AND e.expires_at <= NOW()"
                )
                due = await self._fetchall(
                    conn,
                    "SELECT d.id FROM notification_deliveries d "
                    "JOIN notification_events e ON e.id = d.event_id "
                    "WHERE d.status IN ('pending', 'retry') "
                    "AND d.next_attempt_at <= NOW() "
                    "AND (e.expires_at IS NULL OR e.expires_at > NOW()) "
                    "ORDER BY d.next_attempt_at, d.created_at, d.id "
                    "LIMIT %s FOR UPDATE OF d SKIP LOCKED",
                    (limit,),
                )
                ids = [row["id"] for row in due]
                if not ids:
                    return []
                await conn.execute(
                    "UPDATE notification_deliveries SET status = 'sending', "
                    "attempt_count = attempt_count + 1, lease_until = %s, "
                    "lease_owner = %s, updated_at = NOW() WHERE id = ANY(%s)",
                    (lease_until, worker_id, ids),
                )
                rows = await self._fetchall(
                    conn,
                    "SELECT d.*, e.id AS event__id, e.event_type AS event__event_type, "
                    "e.severity AS event__severity, e.source AS event__source, "
                    "e.title AS event__title, e.body AS event__body, "
                    "e.payload AS event__payload, "
                    "e.idempotency_key AS event__idempotency_key, "
                    "e.correlation_id AS event__correlation_id, "
                    "e.fingerprint AS event__fingerprint, "
                    "e.matched_policy_id AS event__matched_policy_id, "
                    "e.routing_status AS event__routing_status, "
                    "e.occurred_at AS event__occurred_at, "
                    "e.expires_at AS event__expires_at, "
                    "e.created_at AS event__created_at "
                    "FROM notification_deliveries d "
                    "JOIN notification_events e ON e.id = d.event_id "
                    "WHERE d.id = ANY(%s)",
                    (ids,),
                )
        order = {value: index for index, value in enumerate(ids)}
        rows.sort(key=lambda row: order[row["id"]])
        claims: list[DeliveryClaim] = []
        for row in rows:
            event_row = {
                key.removeprefix("event__"): value
                for key, value in row.items()
                if key.startswith("event__")
            }
            delivery_row = {
                key: value for key, value in row.items() if not key.startswith("event__")
            }
            claims.append(
                DeliveryClaim(
                    delivery=_delivery(delivery_row),
                    event=_event(event_row),
                    connector=delivery_row["connector_snapshot"],
                    endpoint=delivery_row["endpoint_snapshot"],
                )
            )
        return claims

    async def mark_delivery_sent(
        self,
        delivery_id: UUID,
        worker_id: str | None = None,
        *,
        provider_message_id: str | None = None,
        expected_attempt_count: int | None = None,
    ) -> bool:
        return await self._finish_delivery(
            delivery_id,
            worker_id,
            status=DeliveryStatus.SENT,
            error=None,
            next_attempt_at=None,
            provider_message_id=provider_message_id,
            expected_attempt_count=expected_attempt_count,
        )

    async def mark_delivery_retry(
        self,
        delivery_id: UUID,
        worker_id: str | None = None,
        *,
        next_attempt_at: datetime,
        error: str | None = None,
        last_error: str | None = None,
        expected_attempt_count: int | None = None,
    ) -> bool:
        return await self._finish_delivery(
            delivery_id,
            worker_id,
            status=DeliveryStatus.RETRY,
            error=last_error if last_error is not None else error,
            next_attempt_at=next_attempt_at,
            provider_message_id=None,
            expected_attempt_count=expected_attempt_count,
        )

    async def mark_delivery_dead(
        self,
        delivery_id: UUID,
        worker_id: str | None = None,
        *,
        error: str | None = None,
        last_error: str | None = None,
        expected_attempt_count: int | None = None,
    ) -> bool:
        return await self._finish_delivery(
            delivery_id,
            worker_id,
            status=DeliveryStatus.DEAD,
            error=last_error if last_error is not None else error,
            next_attempt_at=None,
            provider_message_id=None,
            expected_attempt_count=expected_attempt_count,
        )

    async def _finish_delivery(
        self,
        delivery_id: UUID,
        worker_id: str | None,
        *,
        status: DeliveryStatus,
        error: str | None,
        next_attempt_at: datetime | None,
        provider_message_id: str | None,
        expected_attempt_count: int | None,
    ) -> bool:
        if worker_id is None:
            return False
        sent_at = datetime.now(UTC) if status is DeliveryStatus.SENT else None
        next_due = next_attempt_at or datetime.now(UTC)
        attempt_clause = ""
        attempt_params: tuple[object, ...] = ()
        if expected_attempt_count is not None:
            attempt_clause = " AND attempt_count = %s"
            attempt_params = (expected_attempt_count,)
        async with self.pool.connection() as conn:
            result = await conn.execute(
                "UPDATE notification_deliveries SET status = %s, "
                "next_attempt_at = %s, lease_until = NULL, lease_owner = NULL, "
                "last_error = %s, provider_message_id = %s, sent_at = %s, "
                "updated_at = NOW() WHERE id = %s AND status = 'sending' "
                "AND lease_owner = %s" + attempt_clause,
                (
                    status.value,
                    next_due,
                    error,
                    provider_message_id,
                    sent_at,
                    delivery_id,
                    worker_id,
                    *attempt_params,
                ),
            )
        return result.rowcount == 1
