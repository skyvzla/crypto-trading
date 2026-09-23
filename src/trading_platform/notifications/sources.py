"""Bridge existing durable trading facts into generic notification events."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Awaitable, Callable, Protocol, Sequence
from zoneinfo import ZoneInfo

import httpx
from psycopg.rows import dict_row


logger = logging.getLogger(__name__)


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


@dataclass(frozen=True)
class SourceStateObservation:
    state_key: str
    state: str
    event: SourceNotification | None = None
    publish_from_states: frozenset[str] | None = None


class NotificationSource(Protocol):
    async def recent_signal_events(
        self, *, since: datetime
    ) -> Sequence[dict[str, Any]]: ...

    async def runtime_statuses(self) -> Sequence[dict[str, Any]]: ...

    async def unpublished_order_failures(self) -> Sequence[dict[str, Any]]: ...

    async def hourly_metrics(
        self, *, start: datetime, end: datetime
    ) -> dict[str, int]: ...


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

    async def unpublished_order_failures(self) -> Sequence[dict[str, Any]]:
        async with self.pool.connection() as conn:
            cursor = conn.cursor(row_factory=dict_row)
            await cursor.execute(
                """
                SELECT j.event_id, j.event_time, j.event_type, j.account_id,
                       j.strategy_id, j.symbol, j.client_order_id,
                       j.exchange_order_id, j.details
                FROM execution_event_journal j
                WHERE (
                    j.event_type = 'execution.order_submit_failed'
                    OR (
                      j.event_type = 'execution.order_submit_result'
                      AND j.details->>'status' IN ('REJECTED', 'SUBMIT_UNKNOWN')
                    )
                  )
                  AND j.event_time >= (
                    SELECT (EXTRACT(EPOCH FROM state::timestamptz) * 1000)::BIGINT
                    FROM notification_source_states
                    WHERE state_key = 'notification-order-failure-start'
                  )
                  AND NOT EXISTS (
                    SELECT 1 FROM notification_events n
                    WHERE n.source = 'strategy.' || j.strategy_id
                      AND n.idempotency_key = 'execution-event:' || j.event_id
                  )
                ORDER BY j.event_time, j.id
                """
            )
            failures = list(await cursor.fetchall())
            await cursor.execute(
                """
                SELECT 'ledger-order:' || o.account_id || ':' || o.client_order_id
                         AS event_id,
                       (EXTRACT(EPOCH FROM o.updated_at) * 1000)::BIGINT
                         AS event_time,
                       'ledger.order_rejected' AS event_type,
                       o.account_id, o.strategy_id, o.symbol, o.client_order_id,
                       o.order_id AS exchange_order_id,
                       jsonb_build_object(
                         'status', o.status,
                         'side', o.side,
                         'order_type', o.order_type,
                         'quantity', o.quantity::TEXT,
                         'price', o.price::TEXT,
                         'error_message', '交易所账本记录订单被拒绝'
                       ) AS details
                FROM orders o
                WHERE o.status = 'REJECTED'
                  AND o.updated_at >= (
                    SELECT state::timestamptz
                    FROM notification_source_states
                    WHERE state_key = 'notification-order-failure-start'
                  )
                  AND NOT EXISTS (
                    SELECT 1 FROM execution_event_journal j
                    WHERE j.client_order_id = o.client_order_id
                      AND j.account_id = o.account_id
                      AND (
                        j.event_type = 'execution.order_submit_failed'
                        OR (j.event_type = 'execution.order_submit_result'
                            AND j.details->>'status' IN ('REJECTED', 'SUBMIT_UNKNOWN'))
                      )
                  )
                  AND NOT EXISTS (
                    SELECT 1 FROM notification_events n
                    WHERE n.source = 'strategy.' || o.strategy_id
                      AND n.idempotency_key = 'execution-event:ledger-order:'
                          || o.account_id || ':' || o.client_order_id
                  )
                ORDER BY o.updated_at, o.id
                """
            )
            failures.extend(await cursor.fetchall())
            return sorted(failures, key=lambda row: (row["event_time"], row["event_id"]))

    async def hourly_metrics(
        self, *, start: datetime, end: datetime
    ) -> dict[str, int]:
        start_ms = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)
        async with self.pool.connection() as conn:
            cursor = conn.cursor(row_factory=dict_row)
            await cursor.execute(
                """
                    SELECT
                      (SELECT COUNT(*) FROM strategy_audit_events
                       WHERE event_type = 'signal_triggered'
                         AND created_at >= %s AND created_at < %s) AS signals,
                      (SELECT COUNT(*) FROM execution_event_journal
                       WHERE event_time >= %s AND event_time < %s
                         AND (event_type = 'execution.order_submit_failed'
                           OR (event_type = 'execution.order_submit_result'
                               AND details->>'status' IN ('REJECTED', 'SUBMIT_UNKNOWN')))
                      ) +
                      (SELECT COUNT(*) FROM orders o
                       WHERE o.status = 'REJECTED' AND o.updated_at >= %s
                         AND o.updated_at < %s
                         AND NOT EXISTS (
                           SELECT 1 FROM execution_event_journal j
                           WHERE j.client_order_id = o.client_order_id
                             AND j.account_id = o.account_id
                             AND (j.event_type = 'execution.order_submit_failed'
                               OR (j.event_type = 'execution.order_submit_result'
                                   AND j.details->>'status' IN ('REJECTED', 'SUBMIT_UNKNOWN')))
                         )) AS order_failures,
                      (SELECT COUNT(*) FROM trades
                       WHERE created_at >= %s AND created_at < %s) AS fills,
                      (SELECT COUNT(*) FROM positions WHERE quantity <> 0)
                        AS open_positions,
                      (SELECT COUNT(*) FROM notification_deliveries
                       WHERE status IN ('pending', 'sending')) AS pending_deliveries,
                      (SELECT COUNT(*) FROM notification_deliveries
                       WHERE status = 'retry') AS retry_deliveries,
                      (SELECT COUNT(*) FROM notification_deliveries
                       WHERE status = 'dead') AS dead_deliveries,
                      (SELECT COUNT(*) FROM notification_deliveries
                       WHERE status = 'sent' AND sent_at >= %s AND sent_at < %s)
                        AS sent_deliveries
                """,
                (
                    start,
                    end,
                    start_ms,
                    end_ms,
                    start,
                    end,
                    start,
                    end,
                    start,
                    end,
                ),
            )
            row = await cursor.fetchone()
        assert row is not None
        return {key: int(value) for key, value in row.items()}


class MarketQualitySource:
    """Read the market service quality endpoint without coupling to its hot path."""

    def __init__(
        self,
        api_url: str = "http://market:8000",
        *,
        timeout_seconds: float = 3.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_url = api_url.rstrip("/")
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)
        self._owns_client = client is None

    async def snapshot(self) -> dict[str, Any]:
        response = await self._client.get(f"{self.api_url}/quality")
        try:
            payload = response.json()
        except ValueError as error:
            raise RuntimeError("market quality endpoint returned invalid JSON") from error
        if not isinstance(payload, dict):
            raise RuntimeError("market quality endpoint returned an invalid response")
        payload["http_status"] = response.status_code
        return payload

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


PublishSourceEvent = Callable[[SourceNotification], Awaitable[object]]
ObserveSourceState = Callable[[SourceStateObservation], Awaitable[object]]

_SHANGHAI = ZoneInfo("Asia/Shanghai")


class DomainEventBridge:
    """Translate durable runtime facts into deduplicated notification events."""

    def __init__(
        self,
        source: NotificationSource,
        publish: PublishSourceEvent,
        *,
        signal_lookback: timedelta = timedelta(hours=24),
        signal_ttl: timedelta = timedelta(minutes=15),
        runtime_stale_after: timedelta = timedelta(seconds=45),
        state_observer: ObserveSourceState | None = None,
        market_quality: MarketQualitySource | None = None,
        market_poll_interval: timedelta = timedelta(seconds=30),
        enable_hourly_summary: bool = False,
    ) -> None:
        self.source = source
        self.publish = publish
        self.signal_lookback = signal_lookback
        self.signal_ttl = signal_ttl
        self.runtime_stale_after = runtime_stale_after
        self.state_observer = state_observer
        self.market_quality = market_quality
        self.market_poll_interval = market_poll_interval
        self.enable_hourly_summary = enable_hourly_summary
        self._observed_states: dict[str, str] = {}
        self._last_market_poll: datetime | None = None
        self._market_snapshot: dict[str, Any] | None = None
        self._market_state = "unknown"
        self._last_summary_slot: datetime | None = None

    async def run_once(self, *, now: datetime | None = None) -> int:
        current = _as_utc(now or datetime.now(UTC))
        published = 0
        try:
            signals = await self.source.recent_signal_events(
                since=current - self.signal_lookback
            )
            for row in signals:
                event = self._signal_event(row)
                if event.expires_at is None or event.expires_at > current:
                    await self.publish(event)
                    published += 1
        except Exception:
            logger.exception("notification signal source query failed")

        unpublished_order_failures = getattr(
            self.source, "unpublished_order_failures", None
        )
        if callable(unpublished_order_failures):
            try:
                failures = await unpublished_order_failures()
                for row in failures:
                    await self.publish(self._order_failure_event(row))
                    published += 1
            except Exception:
                logger.exception("notification order failure source query failed")

        runtime_statuses_available = True
        try:
            statuses = await self.source.runtime_statuses()
        except Exception:
            logger.exception("notification runtime status source query failed")
            statuses = ()
            runtime_statuses_available = False
        for row in statuses:
            for observation in self._runtime_observations(row, now=current):
                published += int(await self._observe_state(observation))

        if self.market_quality is not None and self._market_poll_due(current):
            try:
                self._market_snapshot = await self.market_quality.snapshot()
            except Exception as error:
                logger.warning(
                    "market quality source unavailable: %s", type(error).__name__
                )
                self._market_snapshot = {
                    "availability": "unavailable",
                    "error": type(error).__name__,
                }
            self._last_market_poll = current
            observation = self._market_observation(self._market_snapshot, current)
            self._market_state = observation.state
            published += int(await self._observe_state(observation))

        if self.enable_hourly_summary:
            published += await self._publish_hourly_summary(
                statuses,
                current,
                runtime_statuses_available=runtime_statuses_available,
            )
        return published

    async def aclose(self) -> None:
        if self.market_quality is not None:
            await self.market_quality.aclose()

    def _market_poll_due(self, now: datetime) -> bool:
        return (
            self._last_market_poll is None
            or now - self._last_market_poll >= self.market_poll_interval
        )

    async def _observe_state(self, observation: SourceStateObservation) -> bool:
        if self.state_observer is not None:
            return bool(await self.state_observer(observation))

        previous = self._observed_states.get(observation.state_key)
        self._observed_states[observation.state_key] = observation.state
        if previous == observation.state or observation.event is None:
            return False
        if (
            observation.publish_from_states is not None
            and previous not in observation.publish_from_states
        ):
            return False
        await self.publish(observation.event)
        return True

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

    def _order_failure_event(self, row: dict[str, Any]) -> SourceNotification:
        details = dict(row.get("details") or {})
        status = str(details.get("status") or row["event_type"])
        symbol = str(row.get("symbol") or details.get("symbol") or "未知标的")
        strategy_id = str(row.get("strategy_id") or "unknown")
        error_type = details.get("error_type")
        error_message = details.get("error_message")
        payload = details.get("payload")
        exchange_response = (
            payload.get("exchange_response") if isinstance(payload, dict) else None
        )
        exchange_response = (
            exchange_response if isinstance(exchange_response, dict) else {}
        )
        reason = str(
            error_message
            or exchange_response.get("msg")
            or exchange_response.get("code")
            or status
        )[:300]
        intent = details.get("intent")
        intent = intent if isinstance(intent, dict) else details
        side = intent.get("side")
        order_type = intent.get("order_type")
        description = " ".join(
            str(value) for value in (side, order_type) if value
        )
        body = f"策略 {strategy_id} 的 {symbol} 订单提交失败，状态：{status}。"
        if description:
            body += f" 方向/类型：{description}。"
        if error_type:
            body += f" 异常：{error_type}。"
        body += f" 原因：{reason}"
        occurred_at = datetime.fromtimestamp(int(row["event_time"]) / 1000, UTC)
        event_id = str(row["event_id"])
        return SourceNotification(
            event_type="trading.order.failed",
            severity="critical" if status == "SUBMIT_UNKNOWN" else "warning",
            source=f"strategy.{strategy_id}",
            title=f"{symbol} 订单提交失败",
            body=body,
            payload={
                "execution_event_id": event_id,
                "account_id": row["account_id"],
                "strategy_id": strategy_id,
                "symbol": symbol,
                "status": status,
                "client_order_id": row.get("client_order_id"),
                "exchange_order_id": row.get("exchange_order_id"),
                "error_type": error_type,
                "error_message": reason,
            },
            idempotency_key=f"execution-event:{event_id}",
            correlation_id=row.get("client_order_id"),
            fingerprint=f"order-failure:{row.get('account_id')}:{strategy_id}",
            occurred_at=occurred_at,
            expires_at=None,
        )

    def _runtime_observations(
        self, row: dict[str, Any], *, now: datetime
    ) -> tuple[SourceStateObservation, SourceStateObservation]:
        heartbeat_at = _as_utc(row["heartbeat_at"])
        halted = bool(row["halted"])
        status = str(row["status"])
        reason = str(row.get("halt_reason") or status)
        stopped = status == "stopped" or row.get("stopped_at") is not None
        stale = (
            not stopped
            and status in {"running", "degraded"}
            and heartbeat_at < now - self.runtime_stale_after
        )
        if stopped:
            health_state = "stopped"
        elif status == "fatal":
            health_state = "fatal"
        elif stale:
            health_state = "unhealthy"
            reason = "runtime heartbeat is stale"
        elif status == "degraded":
            health_state = "degraded"
        elif status == "running":
            health_state = "healthy"
        else:
            health_state = status

        common_payload = {
            "account_id": row["account_id"],
            "strategy_id": row["strategy_id"],
            "instance_id": row["instance_id"],
            "mode": row["mode"],
            "status": status,
            "halted": halted,
            "halt_reason": row.get("halt_reason"),
            "gate_conditions": dict(row.get("gate_conditions") or {}),
            "heartbeat_at": heartbeat_at.isoformat(),
        }
        identity = ":".join(
            str(row[field]) for field in ("account_id", "strategy_id", "instance_id")
        )
        health_event = None
        recovery_from = None
        if health_state in {"unhealthy", "degraded"}:
            event_type = (
                "system.strategy.unhealthy"
                if health_state == "unhealthy"
                else "system.strategy.degraded"
            )
            severity = "critical" if health_state == "unhealthy" else "warning"
            title = (
                f"{row['strategy_id']} runtime heartbeat lost"
                if health_state == "unhealthy"
                else f"{row['strategy_id']} runtime degraded"
            )
            health_event = self._state_event(
                event_type=event_type,
                severity=severity,
                source=f"strategy.{row['strategy_id']}",
                title=title,
                body=reason,
                payload={**common_payload, "health_state": health_state},
                state_key=f"strategy-health:{identity}",
                state=health_state,
                now=now,
                correlation_id=str(row["instance_id"]),
            )
        elif health_state == "healthy":
            recovery_from = frozenset({"degraded", "unhealthy"})
            health_event = self._state_event(
                event_type="system.strategy.recovered",
                severity="info",
                source=f"strategy.{row['strategy_id']}",
                title=f"{row['strategy_id']} runtime recovered",
                body="策略运行状态已恢复正常。",
                payload={**common_payload, "health_state": health_state},
                state_key=f"strategy-health:{identity}",
                state=health_state,
                now=now,
                correlation_id=str(row["instance_id"]),
            )
        health_observation = SourceStateObservation(
            state_key=f"strategy-health:{identity}",
            state=health_state,
            event=health_event,
            publish_from_states=recovery_from,
        )

        is_halted = halted or status == "fatal"
        risk_state = "halted" if is_halted else "clear"
        risk_event = None
        risk_from = None
        if is_halted:
            risk_event = self._state_event(
                event_type="risk.halted",
                severity="critical",
                source=f"strategy.{row['strategy_id']}",
                title=f"{row['strategy_id']} risk guard halted",
                body=reason,
                payload={**common_payload, "halted": True},
                state_key=f"strategy-risk:{identity}",
                state=risk_state,
                now=now,
                correlation_id=str(row["instance_id"]),
            )
        else:
            risk_from = frozenset({"halted"})
            risk_event = self._state_event(
                event_type="risk.resumed",
                severity="warning",
                source=f"strategy.{row['strategy_id']}",
                title=f"{row['strategy_id']} risk guard resumed",
                body="风控暂停状态已解除。",
                payload={**common_payload, "halted": False},
                state_key=f"strategy-risk:{identity}",
                state=risk_state,
                now=now,
                correlation_id=str(row["instance_id"]),
            )
        risk_observation = SourceStateObservation(
            state_key=f"strategy-risk:{identity}",
            state=risk_state,
            event=risk_event,
            publish_from_states=risk_from,
        )
        return health_observation, risk_observation

    @staticmethod
    def _state_event(
        *,
        event_type: str,
        severity: str,
        source: str,
        title: str,
        body: str,
        payload: dict[str, Any],
        state_key: str,
        state: str,
        now: datetime,
        correlation_id: str | None = None,
    ) -> SourceNotification:
        digest = hashlib.sha256(state_key.encode("utf-8")).hexdigest()
        return SourceNotification(
            event_type=event_type,
            severity=severity,
            source=source,
            title=title,
            body=body,
            payload=payload,
            idempotency_key=f"state:{digest}:{state}:{now.isoformat()}",
            correlation_id=correlation_id,
            fingerprint=f"{event_type}:{digest}",
            occurred_at=now,
            expires_at=None,
        )

    def _market_observation(
        self, snapshot: dict[str, Any], now: datetime
    ) -> SourceStateObservation:
        http_status = snapshot.get("http_status")
        if snapshot.get("availability") == "unavailable" or (
            http_status is not None and http_status not in {200, 503}
        ):
            state = "unavailable"
            unavailable_reason = snapshot.get("error") or (
                f"HTTP {http_status}"
                if http_status is not None
                else "connection failure"
            )
            issues = [f"行情质量接口不可用 ({unavailable_reason})"]
        else:
            issues = []
            for component, values in (
                ("stream", snapshot.get("streams", {})),
                ("pubsub", snapshot.get("pubsub_channels", {})),
                ("metrics", snapshot.get("metrics", {})),
            ):
                if not isinstance(values, dict):
                    continue
                for name, value in sorted(values.items()):
                    if not isinstance(value, dict):
                        continue
                    status = str(value.get("status", "unknown"))
                    if status != "healthy":
                        issue = value.get("issue")
                        detail = f"{component} {name}: {status}"
                        if issue:
                            detail += f" ({issue})"
                        issues.append(detail)
            if not snapshot.get("pubsub_delivery_ready", True) and not issues:
                issues.append("Redis Pub/Sub delivery is not ready")
            if not snapshot.get("ready", False) and not issues:
                issues.append("market quality reports not ready")
            state = (
                "healthy"
                if snapshot.get("ready", False) and not issues
                else "degraded"
            )

        state_key = "market-quality"
        event = None
        recovery_from = None
        if state != "healthy":
            event = self._state_event(
                event_type="market.data.degraded",
                severity="critical" if state == "unavailable" else "warning",
                source="market.quality",
                title="行情数据链路异常",
                body="；".join(issues[:8]),
                payload={"state": state, "issues": issues, "quality": snapshot},
                state_key=state_key,
                state=state,
                now=now,
            )
        else:
            recovery_from = frozenset({"degraded", "unavailable"})
            event = self._state_event(
                event_type="market.data.recovered",
                severity="info",
                source="market.quality",
                title="行情数据链路已恢复",
                body="行情质量状态已恢复正常。",
                payload={"state": state, "quality": snapshot},
                state_key=state_key,
                state=state,
                now=now,
            )
        return SourceStateObservation(
            state_key=state_key,
            state=state,
            event=event,
            publish_from_states=recovery_from,
        )

    async def _publish_hourly_summary(
        self,
        statuses: Sequence[dict[str, Any]],
        now: datetime,
        *,
        runtime_statuses_available: bool,
    ) -> int:
        metrics_reader = getattr(self.source, "hourly_metrics", None)
        if not callable(metrics_reader):
            return 0
        local_slot = now.astimezone(_SHANGHAI).replace(
            minute=0, second=0, microsecond=0
        )
        if local_slot == self._last_summary_slot:
            return 0
        slot_end = local_slot.astimezone(UTC)
        slot_start = (local_slot - timedelta(hours=1)).astimezone(UTC)
        try:
            metrics = await metrics_reader(start=slot_start, end=slot_end)
            event = self._hourly_summary_event(
                statuses=statuses,
                metrics=metrics,
                now=now,
                slot_start=slot_start,
                slot_end=slot_end,
                market_snapshot=self._market_snapshot,
                market_state=self._market_state,
                runtime_statuses_available=runtime_statuses_available,
            )
            observation = SourceStateObservation(
                state_key="hourly-summary",
                state=local_slot.isoformat(),
                event=event,
            )
            published = int(await self._observe_state(observation))
            self._last_summary_slot = local_slot
            return published
        except Exception:
            logger.exception("hourly notification summary failed")
            return 0

    def _hourly_summary_event(
        self,
        *,
        statuses: Sequence[dict[str, Any]],
        metrics: dict[str, int],
        now: datetime,
        slot_start: datetime,
        slot_end: datetime,
        market_snapshot: dict[str, Any] | None,
        market_state: str,
        runtime_statuses_available: bool,
    ) -> SourceNotification:
        strategy_lines: list[str] = []
        severe_runtime = False
        risk_halted = False
        for row in statuses:
            state = self._summary_runtime_state(row, now)
            risk_halted = risk_halted or bool(row["halted"]) or row["status"] == "fatal"
            severe_runtime = severe_runtime or state in {"失联", "降级"}
            strategy_lines.append(
                f"{row['strategy_id']}({row['account_id']}): {state}, "
                f"entry={'开' if row['entry_enabled'] else '关'}"
            )
        if not runtime_statuses_available:
            strategy_lines = ["运行状态读取失败，策略状态未知"]
            severe_runtime = True
        elif not strategy_lines:
            strategy_lines.append("无策略运行状态记录")

        market_issues: list[str] = []
        if market_snapshot is not None:
            for component in ("streams", "pubsub_channels", "metrics"):
                values = market_snapshot.get(component, {})
                if not isinstance(values, dict):
                    continue
                for name, value in sorted(values.items()):
                    if isinstance(value, dict) and value.get("status") != "healthy":
                        market_issues.append(
                            f"{name}:{value.get('status')}"
                        )
        market_line = "健康" if market_state == "healthy" else market_state
        if market_issues:
            market_line += " (" + ", ".join(market_issues[:5]) + ")"

        has_issues = (
            severe_runtime
            or not runtime_statuses_available
            or market_state not in {"healthy", "unknown"}
            or metrics["order_failures"] > 0
            or metrics["dead_deliveries"] > 0
            or metrics["retry_deliveries"] > 0
        )
        severity = "critical" if risk_halted else "warning" if has_issues else "info"
        local_start = slot_start.astimezone(_SHANGHAI)
        local_end = slot_end.astimezone(_SHANGHAI)
        body = "\n".join(
            (
                f"统计时段：{local_start:%Y-%m-%d %H:%M} 至 {local_end:%H:%M}（上海）",
                "策略：" + "；".join(strategy_lines),
                f"行情：{market_line}",
                "本时段："
                f"信号 {metrics['signals']}，订单失败 {metrics['order_failures']}，"
                f"成交 {metrics['fills']}",
                f"当前持仓：{metrics['open_positions']} 个",
                "通知："
                f"已送达 {metrics['sent_deliveries']}，待处理 {metrics['pending_deliveries']}，"
                f"重试 {metrics['retry_deliveries']}，死信 {metrics['dead_deliveries']}",
            )
        )
        payload = {
            "period_start": slot_start.isoformat(),
            "period_end": slot_end.isoformat(),
            "strategies": [
                {
                    "account_id": row["account_id"],
                    "strategy_id": row["strategy_id"],
                    "status": self._summary_runtime_state(row, now),
                    "entry_enabled": bool(row["entry_enabled"]),
                    "halted": bool(row["halted"]),
                }
                for row in statuses
            ],
            "strategy_status_available": runtime_statuses_available,
            "market_state": market_state,
            "market_issues": market_issues,
            **metrics,
        }
        slot_key = local_start.strftime("%Y%m%d%H")
        return SourceNotification(
            event_type="system.hourly_summary",
            severity=severity,
            source="notification.scheduler",
            title=f"系统运行摘要 · {local_start:%m-%d %H:%M}",
            body=body,
            payload=payload,
            idempotency_key=f"hourly-summary:{slot_key}",
            correlation_id=None,
            fingerprint=f"hourly-summary:{slot_key}",
            occurred_at=now,
            expires_at=None,
        )

    def _summary_runtime_state(
        self, row: dict[str, Any], now: datetime
    ) -> str:
        status = str(row["status"])
        if bool(row["halted"]) or status == "fatal":
            return "风控暂停"
        if status == "stopped" or row.get("stopped_at") is not None:
            return "已停止"
        heartbeat_at = _as_utc(row["heartbeat_at"])
        if (
            status in {"running", "degraded"}
            and heartbeat_at < now - self.runtime_stale_after
        ):
            return "失联"
        if status == "degraded":
            return "降级"
        if status == "running":
            return "运行中"
        return status


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
