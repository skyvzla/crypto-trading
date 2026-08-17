"""Bridge existing durable trading facts into generic notification events."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Awaitable, Callable, Protocol, Sequence

from psycopg.rows import dict_row


@dataclass(frozen=True)
class SourceNotification:
    event_type: str
    severity: str
    source: str
    title: str
    body: str
    payload: dict[str, Any]
    idempotency_key: str
    correlation_id: str | None
    fingerprint: str | None
    occurred_at: datetime
    expires_at: datetime | None


class NotificationSource(Protocol):
    async def recent_signal_events(
        self, *, since: datetime
    ) -> Sequence[dict[str, Any]]: ...

    async def runtime_statuses(self) -> Sequence[dict[str, Any]]: ...


class PostgresNotificationSource:
    """Read only the existing PostgreSQL business facts used for alerting."""

    def __init__(self, pool: object) -> None:
        self.pool = pool

    async def recent_signal_events(
        self, *, since: datetime
    ) -> Sequence[dict[str, Any]]:
        async with self.pool.connection() as conn:
            cursor = conn.cursor(row_factory=dict_row)
            await cursor.execute(
                """
                SELECT event_key, account_id, event_time, event_type, symbol,
                       strategy_id, campaign_id, details, created_at
                FROM strategy_audit_events
                WHERE event_type = 'signal_triggered' AND created_at >= %s
                ORDER BY created_at, id
                """,
                (since,),
            )
            return await cursor.fetchall()

    async def runtime_statuses(self) -> Sequence[dict[str, Any]]:
        async with self.pool.connection() as conn:
            cursor = conn.cursor(row_factory=dict_row)
            await cursor.execute(
                """
                SELECT account_id, strategy_id, instance_id, mode, status,
                       entry_enabled, halted, halt_reason, gate_conditions,
                       started_at, heartbeat_at, stopped_at
                FROM strategy_runtime_status
                ORDER BY account_id, strategy_id
                """
            )
            return await cursor.fetchall()


PublishSourceEvent = Callable[[SourceNotification], Awaitable[object]]


class DomainEventBridge:
    """Translate durable domain records without touching the trading hot path."""

    def __init__(
        self,
        source: NotificationSource,
        publish: PublishSourceEvent,
        *,
        signal_lookback: timedelta = timedelta(hours=24),
        signal_ttl: timedelta = timedelta(minutes=15),
        runtime_stale_after: timedelta = timedelta(seconds=45),
    ) -> None:
        self.source = source
        self.publish = publish
        self.signal_lookback = signal_lookback
        self.signal_ttl = signal_ttl
        self.runtime_stale_after = runtime_stale_after

    async def run_once(self, *, now: datetime | None = None) -> int:
        current = now or datetime.now(UTC)
        events: list[SourceNotification] = []
        signals = await self.source.recent_signal_events(
            since=current - self.signal_lookback
        )
        events.extend(
            event
            for row in signals
            if (event := self._signal_event(row)).expires_at is None
            or event.expires_at > current
        )
        statuses = await self.source.runtime_statuses()
        events.extend(
            event
            for row in statuses
            if (event := self._runtime_event(row, now=current)) is not None
        )
        for event in events:
            await self.publish(event)
        return len(events)

    def _signal_event(self, row: dict[str, Any]) -> SourceNotification:
        event_time = datetime.fromtimestamp(int(row["event_time"]) / 1000, UTC)
        details = dict(row.get("details") or {})
        symbol = str(row["symbol"])
        strategy_id = str(row["strategy_id"])
        campaign_id = row.get("campaign_id")
        trigger_price = details.get("trigger_price")
        body = f"{strategy_id} detected a confirmed signal for {symbol}."
        if trigger_price is not None:
            body += f" Trigger price: {trigger_price}."
        return SourceNotification(
            event_type="trading.signal.triggered",
            severity="warning",
            source=f"strategy.{strategy_id}",
            title=f"{symbol} trading signal",
            body=body,
            payload={
                "account_id": row["account_id"],
                "strategy_id": strategy_id,
                "symbol": symbol,
                "campaign_id": campaign_id,
                **details,
            },
            idempotency_key=f"strategy-audit:{row['event_key']}",
            correlation_id=None if campaign_id is None else str(campaign_id),
            fingerprint=f"signal:{strategy_id}:{symbol}",
            occurred_at=event_time,
            expires_at=event_time + self.signal_ttl,
        )

    def _runtime_event(
        self, row: dict[str, Any], *, now: datetime
    ) -> SourceNotification | None:
        heartbeat_at = row["heartbeat_at"]
        if heartbeat_at.tzinfo is None:
            heartbeat_at = heartbeat_at.replace(tzinfo=UTC)
        stale = heartbeat_at < now - self.runtime_stale_after
        halted = bool(row["halted"])
        status = str(row["status"])
        reason = str(row.get("halt_reason") or status)
        if halted or status == "fatal":
            event_type = "risk.halted"
            severity = "critical"
            title = f"{row['strategy_id']} risk guard halted"
        elif stale:
            event_type = "system.strategy.unhealthy"
            severity = "critical"
            reason = "runtime heartbeat is stale"
            title = f"{row['strategy_id']} runtime heartbeat lost"
        elif status == "degraded":
            event_type = "system.strategy.degraded"
            severity = "warning"
            title = f"{row['strategy_id']} runtime degraded"
        else:
            return None

        identity = "|".join(
            (
                str(row["account_id"]),
                str(row["strategy_id"]),
                str(row["instance_id"]),
                event_type,
                reason,
            )
        )
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        return SourceNotification(
            event_type=event_type,
            severity=severity,
            source=f"strategy.{row['strategy_id']}",
            title=title,
            body=reason,
            payload={
                "account_id": row["account_id"],
                "strategy_id": row["strategy_id"],
                "instance_id": row["instance_id"],
                "mode": row["mode"],
                "status": status,
                "halted": halted,
                "halt_reason": row.get("halt_reason"),
                "gate_conditions": dict(row.get("gate_conditions") or {}),
                "heartbeat_at": heartbeat_at.isoformat(),
            },
            idempotency_key=f"runtime:{digest}",
            correlation_id=str(row["instance_id"]),
            fingerprint=(
                f"runtime:{row['account_id']}:{row['strategy_id']}:{event_type}"
            ),
            occurred_at=heartbeat_at,
            expires_at=None,
        )
