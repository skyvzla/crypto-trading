"""Execution event journal PostgreSQL and API contract tests."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import urllib.parse
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from psycopg import sql
from psycopg.errors import CheckViolation, RaiseException, UniqueViolation

from trading_platform.ledger.api.routes import router
from trading_platform.ledger.db.migrations import apply_migrations
from trading_platform.ledger.db.models import LedgerDB, create_connection_pool
from trading_platform.shared.execution_event_journal import ExecutionEvent


pytestmark = pytest.mark.skipif(
    not os.getenv("LEDGER_TEST_DSN"), reason="LEDGER_TEST_DSN not set"
)


@pytest.fixture
async def ledger():
    base_dsn = os.environ["LEDGER_TEST_DSN"]
    schema = f"execution_events_{uuid4().hex}"
    admin_pool = await create_connection_pool(base_dsn, 1, 2)
    try:
        async with admin_pool.connection() as connection:
            await connection.execute(
                sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema))
            )
    finally:
        await admin_pool.close()

    separator = "&" if "?" in base_dsn else "?"
    dsn = base_dsn + separator + "options=" + urllib.parse.quote(
        f"-csearch_path={schema}"
    )
    pool = await create_connection_pool(dsn, 1, 4)
    try:
        await apply_migrations(pool, schema=schema)
        yield LedgerDB(pool)
    finally:
        await pool.close()
        cleanup_pool = await create_connection_pool(base_dsn, 1, 2)
        try:
            async with cleanup_pool.connection() as connection:
                await connection.execute(
                    sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
                )
        finally:
            await cleanup_pool.close()


def make_event(
    suffix: str,
    sequence: int,
    *,
    event_id: str | None = None,
    run_id: str = "run-1",
    event_time: int | None = None,
    account_id: str = "account-1",
    strategy_id: str = "spike_short",
    event_type: str = "order.intent_recorded",
    source: str = "spike.execution",
    severity: str = "info",
    trace_id: str = "trace-1",
    symbol: str | None = "BTCUSDT",
    campaign_id: str | None = "campaign-1",
    client_order_id: str | None = "client-1",
    exchange_order_id: str | None = None,
    idempotency_key: str | None = None,
    details: dict[str, Any] | None = None,
) -> ExecutionEvent:
    return ExecutionEvent(
        event_id=event_id or uuid4().hex,
        run_id=run_id,
        sequence=sequence,
        event_time=event_time if event_time is not None else sequence * 100,
        event_type=event_type,
        source=source,
        severity=severity,
        account_id=account_id,
        strategy_id=strategy_id,
        trace_id=trace_id,
        causation_id=None,
        symbol=symbol,
        campaign_id=campaign_id,
        client_order_id=client_order_id,
        exchange_order_id=exchange_order_id,
        details={} if details is None else details,
        idempotency_key=idempotency_key,
    )


@pytest.mark.asyncio
async def test_migration_creates_journal_indexes_trigger_and_constraints(ledger):
    expected_indexes = {
        "idx_execution_event_journal_account_time",
        "idx_execution_event_journal_strategy_time",
        "idx_execution_event_journal_run_sequence",
        "idx_execution_event_journal_trace_time",
        "idx_execution_event_journal_event_type_time",
        "idx_execution_event_journal_source_time",
        "idx_execution_event_journal_symbol_time",
        "idx_execution_event_journal_campaign_time",
        "idx_execution_event_journal_client_order_time",
        "idx_execution_event_journal_exchange_order_time",
        "idx_execution_event_journal_event_time",
        "idx_execution_event_journal_idempotency_key",
        "execution_event_journal_snapshot_domain_key",
    }
    async with ledger.pool.connection() as connection:
        table = await (
            await connection.execute(
                "SELECT to_regclass('execution_event_journal')"
            )
        ).fetchone()
        indexes = await (
            await connection.execute(
                "SELECT indexname FROM pg_indexes "
                "WHERE tablename = 'execution_event_journal'"
            )
        ).fetchall()
        constraints = await (
            await connection.execute(
                "SELECT conname FROM pg_constraint "
                "WHERE conrelid = 'execution_event_journal'::regclass "
                "AND contype = 'u' ORDER BY conname"
            )
        ).fetchall()
        triggers = await (
            await connection.execute(
                "SELECT tgname, pg_get_triggerdef(oid) "
                "FROM pg_trigger WHERE tgrelid = 'execution_event_journal'::regclass "
                "AND NOT tgisinternal ORDER BY tgname"
            )
        ).fetchall()

    assert table == ("execution_event_journal",)
    assert expected_indexes <= {row[0] for row in indexes}
    assert [row[0] for row in constraints] == [
        "execution_event_journal_event_id_key",
        "execution_event_journal_run_sequence_key",
    ]
    trigger_definitions = dict(triggers)
    assert set(trigger_definitions) == {
        "execution_event_journal_no_truncate",
        "execution_event_journal_no_update_delete",
    }
    assert "BEFORE DELETE OR UPDATE" in trigger_definitions[
        "execution_event_journal_no_update_delete"
    ]
    assert "BEFORE TRUNCATE" in trigger_definitions[
        "execution_event_journal_no_truncate"
    ]
    assert "FOR EACH STATEMENT" in trigger_definitions[
        "execution_event_journal_no_truncate"
    ]


@pytest.mark.asyncio
async def test_batch_insert_is_idempotent_and_persists_canonical_payload(ledger):
    suffix = uuid4().hex[:10]
    events = [
        make_event(
            suffix,
            1,
            details={"z": [2, True], "a": "value"},
        ),
        make_event(suffix, 2, event_time=50),
    ]

    assert await ledger.insert_execution_events(events) == 2
    assert await ledger.insert_execution_events(events) == 0

    items, total = await ledger.list_execution_events(run_id="run-1")
    assert total == 2
    assert [item.event_id for item in items] == [events[0].event_id, events[1].event_id]
    items, total = await ledger.list_execution_events(account_id="account-1")
    assert total == 2
    assert [item.event_id for item in items] == [events[1].event_id, events[0].event_id]
    first = next(item for item in items if item.event_id == events[0].event_id)
    assert first.details == {"a": "value", "z": [2, True]}
    expected_hash = hashlib.sha256(
        json.dumps(
            events[0].to_dict(),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    assert first.payload_hash == expected_hash
    assert len(first.payload_hash) == 64
    assert first.received_at is not None


@pytest.mark.asyncio
async def test_filters_time_bounds_pagination_and_stable_order(ledger):
    suffix = uuid4().hex[:10]
    events = [
        make_event(suffix, 1, event_time=200, account_id=f"account-{suffix}"),
        make_event(
            suffix,
            2,
            event_time=100,
            account_id=f"account-{suffix}",
            event_type="order.submit_started",
        ),
        make_event(
            suffix,
            3,
            event_time=150,
            account_id=f"account-{suffix}",
            symbol="ETHUSDT",
        ),
    ]
    assert await ledger.insert_execution_events(events) == 3

    items, total = await ledger.list_execution_events(
        account_id=f"account-{suffix}",
        event_time_from=100,
        event_time_to=150,
        limit=1,
        offset=0,
    )
    assert total == 2
    assert [item.event_time for item in items] == [100]

    items, total = await ledger.list_execution_events(
        account_id=f"account-{suffix}",
        event_type="order.submit_started",
        event_time_start=100,
        event_time_end=200,
        limit=100,
    )
    assert total == 1
    assert [item.event_id for item in items] == [events[1].event_id]

    items, total = await ledger.list_execution_events(
        account_id=f"account-{suffix}",
        start_event_time=100,
        end_event_time=200,
        limit=2,
        offset=1,
    )
    assert total == 3
    assert [item.event_time for item in items] == [150, 200]

    with pytest.raises(ValueError, match="must not be after"):
        await ledger.list_execution_events(
            account_id=f"account-{suffix}", event_time_from=201, event_time_to=200
        )


@pytest.mark.asyncio
async def test_conflicting_event_id_is_rejected_and_batch_is_atomic(ledger):
    suffix = uuid4().hex[:10]
    original = make_event(suffix, 1)
    assert await ledger.insert_execution_events([original]) == 1

    conflicting = make_event(
        suffix,
        1,
        event_id=original.event_id,
        details={"changed": True},
    )
    with pytest.raises(ValueError, match="conflicts"):
        await ledger.insert_execution_events(
            [make_event(suffix, 2), conflicting]
        )

    items, total = await ledger.list_execution_events(run_id="run-1")
    assert total == 1
    assert [item.event_id for item in items] == [original.event_id]


@pytest.mark.asyncio
async def test_snapshot_lifecycle_domain_key_replays_across_runs_and_rejects_conflicts(
    ledger,
):
    suffix = uuid4().hex[:10]
    first = make_event(
        suffix,
        1,
        run_id="snapshot-run-1",
        event_type="market.snapshot_completed",
        source="spike.market_snapshot",
        trace_id="snapshot-1",
        campaign_id=f"campaign-{suffix}",
        client_order_id=None,
        details={"snapshot_id": f"snapshot-{suffix}", "sha256": "a" * 64},
        idempotency_key=f"market.snapshot_completed:snapshot-{suffix}",
    )
    replay = make_event(
        suffix,
        1,
        run_id="snapshot-run-2",
        event_type=first.event_type,
        source=first.source,
        trace_id=first.trace_id,
        campaign_id=first.campaign_id,
        client_order_id=None,
        details=first.details,
        idempotency_key=first.idempotency_key,
    )

    assert await ledger.insert_execution_events([first]) == 1
    assert await ledger.insert_execution_events([replay]) == 0
    _, total = await ledger.list_execution_events(
        event_type="market.snapshot_completed",
        campaign_id=first.campaign_id,
    )
    assert total == 1

    conflicting = make_event(
        suffix,
        1,
        run_id="snapshot-run-3",
        event_type=first.event_type,
        source=first.source,
        trace_id=first.trace_id,
        campaign_id=first.campaign_id,
        client_order_id=None,
        details={"snapshot_id": f"snapshot-{suffix}", "sha256": "b" * 64},
        idempotency_key=first.idempotency_key,
    )
    with pytest.raises(ValueError, match="idempotency key"):
        await ledger.insert_execution_events([conflicting])


@pytest.mark.asyncio
async def test_snapshot_domain_row_rejects_same_event_id_with_changed_run_and_time(
    ledger,
):
    suffix = uuid4().hex[:10]
    snapshot_id = f"snapshot-{suffix}"
    stored = make_event(
        suffix,
        1,
        event_id=f"event-{suffix}",
        run_id=f"snapshot-run-{suffix}",
        event_time=100,
        event_type="market.snapshot_completed",
        source="spike.market_snapshot",
        trace_id=snapshot_id,
        client_order_id=None,
        details={"snapshot_id": snapshot_id, "sha256": "a" * 64},
        idempotency_key=f"market.snapshot_completed:{snapshot_id}",
    )
    changed = make_event(
        suffix,
        stored.sequence,
        event_id=stored.event_id,
        run_id=f"restarted-run-{suffix}",
        event_time=200,
        event_type=stored.event_type,
        source=stored.source,
        trace_id=stored.trace_id,
        client_order_id=None,
        details=stored.details,
        idempotency_key=stored.idempotency_key,
    )

    assert await ledger.insert_execution_events([stored]) == 1
    with pytest.raises(ValueError, match="conflicts with an existing payload"):
        await ledger.insert_execution_events([changed])


@pytest.mark.asyncio
async def test_snapshot_domain_row_rejects_new_event_id_reusing_run_sequence(ledger):
    suffix = uuid4().hex[:10]
    snapshot_id = f"snapshot-{suffix}"
    stored = make_event(
        suffix,
        1,
        event_id=f"event-{suffix}-stored",
        run_id=f"snapshot-run-{suffix}",
        event_type="market.snapshot_completed",
        source="spike.market_snapshot",
        trace_id=snapshot_id,
        client_order_id=None,
        details={"snapshot_id": snapshot_id, "sha256": "a" * 64},
        idempotency_key=f"market.snapshot_completed:{snapshot_id}",
    )
    changed = make_event(
        suffix,
        stored.sequence,
        event_id=f"event-{suffix}-changed",
        run_id=stored.run_id,
        event_time=stored.event_time,
        event_type=stored.event_type,
        source=stored.source,
        trace_id=stored.trace_id,
        client_order_id=None,
        details=stored.details,
        idempotency_key=stored.idempotency_key,
    )

    assert await ledger.insert_execution_events([stored]) == 1
    with pytest.raises(ValueError, match="existing run/sequence identity"):
        await ledger.insert_execution_events([changed])


@pytest.mark.asyncio
async def test_snapshot_domain_row_accepts_complete_same_event_id_replay(ledger):
    suffix = uuid4().hex[:10]
    snapshot_id = f"snapshot-{suffix}"
    event = make_event(
        suffix,
        1,
        event_id=f"event-{suffix}",
        run_id=f"snapshot-run-{suffix}",
        event_type="market.snapshot_completed",
        source="spike.market_snapshot",
        trace_id=snapshot_id,
        client_order_id=None,
        details={"snapshot_id": snapshot_id, "sha256": "a" * 64},
        idempotency_key=f"market.snapshot_completed:{snapshot_id}",
    )

    assert await ledger.insert_execution_events([event]) == 1
    assert await ledger.insert_execution_events([event]) == 0


@pytest.mark.asyncio
async def test_snapshot_domain_row_accepts_new_event_and_run_identity_replay(ledger):
    suffix = uuid4().hex[:10]
    snapshot_id = f"snapshot-{suffix}"
    first = make_event(
        suffix,
        1,
        event_id=f"event-{suffix}-first",
        run_id=f"snapshot-run-{suffix}-first",
        event_type="market.snapshot_completed",
        source="spike.market_snapshot",
        trace_id=snapshot_id,
        client_order_id=None,
        details={"snapshot_id": snapshot_id, "sha256": "a" * 64},
        idempotency_key=f"market.snapshot_completed:{snapshot_id}",
    )
    replay = make_event(
        suffix,
        2,
        event_id=f"event-{suffix}-replay",
        run_id=f"snapshot-run-{suffix}-replay",
        event_time=200,
        event_type=first.event_type,
        source=first.source,
        trace_id=first.trace_id,
        client_order_id=None,
        details=first.details,
        idempotency_key=first.idempotency_key,
    )

    assert await ledger.insert_execution_events([first]) == 1
    assert await ledger.insert_execution_events([replay]) == 0


@pytest.mark.asyncio
async def test_concurrent_snapshot_domain_replay_is_atomic_and_validates_payload(
    ledger,
):
    """Two independent pool connections must converge on one domain row.

    The trigger deliberately keeps the first INSERT in flight after taking a
    transaction advisory lock.  The second INSERT therefore reaches the
    partial unique index while the first transaction is still active, which
    exercises PostgreSQL's conflict arbiter instead of relying on scheduler
    luck to create overlap.
    """

    suffix = uuid4().hex[:10]
    snapshot_id = f"snapshot-{suffix}"
    key = f"market.snapshot_completed:{snapshot_id}"
    first = make_event(
        suffix,
        1,
        event_id=f"event-{suffix}-first",
        run_id=f"snapshot-run-{suffix}-first",
        event_type="market.snapshot_completed",
        source="spike.market_snapshot",
        trace_id=f"trace-{suffix}",
        campaign_id=f"campaign-{suffix}",
        client_order_id=None,
        details={"snapshot_id": snapshot_id, "sha256": "a" * 64},
        idempotency_key=key,
    )
    replay = make_event(
        suffix,
        1,
        event_id=f"event-{suffix}-replay",
        run_id=f"snapshot-run-{suffix}-replay",
        event_type=first.event_type,
        source=first.source,
        trace_id=first.trace_id,
        campaign_id=first.campaign_id,
        client_order_id=None,
        details=first.details,
        idempotency_key=key,
    )

    async with ledger.pool.connection() as connection:
        await connection.execute(
            """
            CREATE FUNCTION snapshot_domain_insert_gate()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $gate$
            BEGIN
                IF NEW.event_type IN (
                    'market.snapshot_completed', 'market.snapshot_failed'
                ) AND NEW.idempotency_key IS NOT NULL THEN
                    PERFORM pg_advisory_xact_lock(
                        hashtextextended(
                            NEW.event_type || ':' || NEW.idempotency_key, 0
                        )
                    );
                    PERFORM pg_sleep(0.2);
                END IF;
                RETURN NEW;
            END;
            $gate$
            """
        )
        await connection.execute(
            """
            CREATE TRIGGER snapshot_domain_insert_gate
            BEFORE INSERT ON execution_event_journal
            FOR EACH ROW EXECUTE FUNCTION snapshot_domain_insert_gate()
            """
        )

    try:
        results = await asyncio.gather(
            ledger.insert_execution_events([first]),
            ledger.insert_execution_events([replay]),
        )
    finally:
        async with ledger.pool.connection() as connection:
            await connection.execute(
                "DROP TRIGGER snapshot_domain_insert_gate "
                "ON execution_event_journal"
            )
            await connection.execute("DROP FUNCTION snapshot_domain_insert_gate()")

    assert sorted(results) == [0, 1]
    _, total = await ledger.list_execution_events(
        event_type="market.snapshot_completed",
        campaign_id=first.campaign_id,
    )
    assert total == 1

    conflicting = make_event(
        suffix,
        1,
        event_id=f"event-{suffix}-conflicting",
        run_id=f"snapshot-run-{suffix}-conflicting",
        event_type=first.event_type,
        source=first.source,
        trace_id=first.trace_id,
        campaign_id=first.campaign_id,
        client_order_id=None,
        details={"snapshot_id": snapshot_id, "sha256": "b" * 64},
        idempotency_key=key,
    )
    with pytest.raises(ValueError, match="idempotency key"):
        await ledger.insert_execution_events([conflicting])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("identity", "expected_message"),
    (
        ("event_id", "conflicts with an existing payload"),
        ("run_sequence", "existing run/sequence identity"),
    ),
)
async def test_snapshot_domain_replay_rejects_other_identity_collision(
    ledger, identity, expected_message
):
    suffix = uuid4().hex[:10]
    snapshot_id = f"snapshot-{suffix}"
    key = f"market.snapshot_completed:{snapshot_id}"
    stored = make_event(
        suffix,
        1,
        event_id=f"event-{suffix}-stored",
        run_id=f"snapshot-run-{suffix}-stored",
        event_type="market.snapshot_completed",
        source="spike.market_snapshot",
        trace_id=f"trace-{suffix}",
        campaign_id=f"campaign-{suffix}",
        client_order_id=None,
        details={"snapshot_id": snapshot_id, "sha256": "a" * 64},
        idempotency_key=key,
    )
    proposed = make_event(
        suffix,
        2,
        event_id=f"event-{suffix}-proposed",
        run_id=f"snapshot-run-{suffix}-proposed",
        event_type=stored.event_type,
        source=stored.source,
        trace_id=stored.trace_id,
        campaign_id=stored.campaign_id,
        client_order_id=None,
        details=stored.details,
        idempotency_key=key,
    )
    blocker = make_event(
        suffix,
        3 if identity == "event_id" else proposed.sequence,
        event_id=(
            proposed.event_id
            if identity == "event_id"
            else f"event-{suffix}-blocker"
        ),
        run_id=(
            f"ordinary-run-{suffix}"
            if identity == "event_id"
            else proposed.run_id
        ),
        event_type="order.intent_recorded",
    )

    assert await ledger.insert_execution_events([stored, blocker]) == 2
    with pytest.raises(ValueError, match=expected_message):
        await ledger.insert_execution_events([proposed])

    _, total = await ledger.list_execution_events(
        event_type="market.snapshot_completed",
        campaign_id=stored.campaign_id,
    )
    assert total == 1


@pytest.mark.asyncio
async def test_snapshot_domain_replay_deduplicates_legacy_unkeyed_event(ledger):
    suffix = uuid4().hex[:10]
    snapshot_id = f"snapshot-{suffix}"
    legacy = make_event(
        suffix,
        1,
        event_type="market.snapshot_failed",
        source="spike.market_snapshot",
        trace_id=snapshot_id,
        details={"snapshot_id": snapshot_id, "reason": "orphan"},
    )
    await ledger.insert_execution_events([legacy])

    replay = make_event(
        suffix,
        1,
        run_id="restarted-run",
        event_type=legacy.event_type,
        source=legacy.source,
        trace_id=legacy.trace_id,
        details=legacy.details,
        idempotency_key=f"market.snapshot_failed:{snapshot_id}",
    )
    assert await ledger.insert_execution_events([replay]) == 0
    _, total = await ledger.list_execution_events(
        event_type="market.snapshot_failed",
        campaign_id=legacy.campaign_id,
    )
    assert total == 1


@pytest.mark.asyncio
async def test_unique_run_sequence_failure_rolls_back_prior_batch_rows(ledger):
    suffix = uuid4().hex[:10]
    first = make_event(suffix, 1)
    conflicting_sequence = make_event(
        suffix, 1, event_id=uuid4().hex, event_type="order.rejected"
    )

    with pytest.raises(UniqueViolation):
        await ledger.insert_execution_events([first, conflicting_sequence])

    _, total = await ledger.list_execution_events(run_id="run-1")
    assert total == 0


@pytest.mark.asyncio
async def test_journal_is_append_only_at_database_boundary(ledger):
    suffix = uuid4().hex[:10]
    event = make_event(suffix, 1)
    assert await ledger.insert_execution_events([event]) == 1

    with pytest.raises(RaiseException, match="append-only"):
        async with ledger.pool.connection() as connection:
            await connection.execute(
                "UPDATE execution_event_journal SET severity = 'warning' "
                "WHERE event_id = %s",
                (event.event_id,),
            )

    with pytest.raises(RaiseException, match="append-only"):
        async with ledger.pool.connection() as connection:
            await connection.execute(
                "DELETE FROM execution_event_journal WHERE event_id = %s",
                (event.event_id,),
            )

    with pytest.raises(RaiseException, match="append-only"):
        async with ledger.pool.connection() as connection:
            await connection.execute("TRUNCATE execution_event_journal")

    _, total = await ledger.list_execution_events(run_id="run-1")
    assert total == 1


@pytest.mark.asyncio
async def test_journal_constraints_reject_invalid_sequence_and_event_time(ledger):
    async with ledger.pool.connection() as connection:
        with pytest.raises(CheckViolation, match="sequence"):
            await connection.execute(
                "INSERT INTO execution_event_journal ("
                "event_id, run_id, sequence, event_time, event_type, source, "
                "severity, account_id, strategy_id, trace_id, details, payload_hash"
                ") VALUES ("
                "'invalid-sequence', 'run-constraints', 0, 0, 'test', 'test', "
                "'info', 'account-constraints', 'strategy', 'trace', '{}'::jsonb, "
                "'0000000000000000000000000000000000000000000000000000000000000000'"
                ")"
            )

    async with ledger.pool.connection() as connection:
        with pytest.raises(CheckViolation, match="event_time"):
            await connection.execute(
                "INSERT INTO execution_event_journal ("
                "event_id, run_id, sequence, event_time, event_type, source, "
                "severity, account_id, strategy_id, trace_id, details, payload_hash"
                ") VALUES ("
                "'invalid-event-time', 'run-constraints', 1, -1, 'test', 'test', "
                "'info', 'account-constraints', 'strategy', 'trace', '{}'::jsonb, "
                "'0000000000000000000000000000000000000000000000000000000000000000'"
                ")"
            )


@pytest.mark.asyncio
async def test_execution_events_api_exposes_filters_pagination_and_422(
    ledger, monkeypatch
):
    query_token = "execution-event-query-test-token"
    monkeypatch.setenv("EXECUTION_EVENT_QUERY_TOKEN", query_token)
    suffix = uuid4().hex[:10]
    account = f"api-account-{suffix}"
    events = [
        make_event(
            suffix,
            1,
            account_id=account,
            event_time=2_000,
            event_type="market.snapshot_completed",
            source="spike.market_snapshot",
            idempotency_key=f"api-event:{suffix}",
            details={"decision": "first"},
        ),
        make_event(
            suffix,
            2,
            account_id=account,
            event_time=1_000,
            details={"decision": "second"},
        ),
    ]
    assert await ledger.insert_execution_events(events) == 2

    app = FastAPI()
    app.state.ledger_db = ledger
    app.include_router(router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/api/v1/execution-events",
            headers={"Authorization": f"Bearer {query_token}"},
            params={
                "account_id": account,
                "run_id": "run-1",
                "limit": 10,
            },
        )
        invalid = await client.get(
            "/api/v1/execution-events",
            headers={"Authorization": f"Bearer {query_token}"},
            params={"event_time_from": 2_000, "event_time_to": 1_000},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 2
    assert payload["limit"] == 10
    assert payload["offset"] == 0
    assert [item["event_id"] for item in payload["items"]] == [
        events[0].event_id,
        events[1].event_id,
    ]
    assert [item["sequence"] for item in payload["items"]] == [1, 2]
    assert payload["items"][0]["idempotency_key"] == f"api-event:{suffix}"
    assert payload["items"][1]["idempotency_key"] is None
    assert payload["items"][0]["details"] == {"decision": "first"}
    assert query_token not in response.text
    assert invalid.status_code == 422
    assert "must not be after" in invalid.json()["detail"]


@pytest.mark.asyncio
async def test_execution_events_api_requires_configured_bearer_token(
    monkeypatch, caplog
):
    query_token = "execution-event-query-secret-value"
    query = AsyncMock(side_effect=AssertionError("query must not run"))
    db = type("QueryGuardDB", (), {"list_execution_events": query})()
    app = FastAPI()
    app.state.ledger_db = db
    app.include_router(router)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        monkeypatch.delenv("EXECUTION_EVENT_QUERY_TOKEN", raising=False)
        unavailable = await client.get(
            "/api/v1/execution-events",
            headers={"Authorization": f"Bearer {query_token}"},
        )

        monkeypatch.setenv("EXECUTION_EVENT_QUERY_TOKEN", query_token)
        missing = await client.get("/api/v1/execution-events")
        wrong = await client.get(
            "/api/v1/execution-events",
            headers={"Authorization": "Bearer wrong-token"},
        )
        non_ascii = await client.get(
            "/api/v1/execution-events",
            headers=[(b"authorization", b"Bearer \xff")],
        )

    assert unavailable.status_code == 503
    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert non_ascii.status_code == 401
    assert missing.headers["www-authenticate"] == "Bearer"
    assert wrong.headers["www-authenticate"] == "Bearer"
    assert non_ascii.headers["www-authenticate"] == "Bearer"
    query.assert_not_awaited()
    observed_text = " ".join(
        [
            unavailable.text,
            missing.text,
            wrong.text,
            non_ascii.text,
            caplog.text,
        ]
    )
    assert query_token not in observed_text
    assert "wrong-token" not in observed_text
