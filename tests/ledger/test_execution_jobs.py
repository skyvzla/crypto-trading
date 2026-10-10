import os
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from trading_platform.ledger.db.migrations import apply_migrations
from trading_platform.ledger.db.models import create_connection_pool
from trading_platform.ledger.execution_jobs import ExecutionJobStore
from trading_platform.shared.events import OrderIntent
from trading_platform.strategies.execution_queue import ExecutionQueue, prepare_recovered_exit


@pytest.fixture
async def store():
    dsn = os.getenv("LEDGER_TEST_DSN")
    if not dsn:
        pytest.skip("LEDGER_TEST_DSN is required")
    pool = await create_connection_pool(dsn, 1, 2)
    await apply_migrations(pool)
    instance = ExecutionJobStore(pool, "jobs-" + uuid4().hex, "long_breakout")
    try:
        yield instance
    finally:
        async with pool.connection() as connection:
            await connection.execute("DELETE FROM strategy_execution_jobs WHERE account_id = %s", (instance.account_id,))
        await pool.close()


def exit_intent():
    return OrderIntent("BTCUSDT", "SELL", Decimal("100"), Decimal("1"), "exit-1",
                       order_type="MARKET", reduce_only=True, campaign_id="campaign-1")


async def test_job_identity_is_durable_and_conflicting_intent_is_rejected(store):
    intent = exit_intent()
    assert await store.save("exit", intent, 1000)
    assert await store.save("exit", intent, 1000)
    assert len(await store.pending()) == 1
    with pytest.raises(ValueError, match="identity conflicts"):
        await store.save("exit", replace(intent, quantity=Decimal("2")), 1000)


async def test_dequeued_exit_survives_restart_and_rechecks_position(store):
    first = ExecutionQueue()
    first.store = store
    await first.put("exit", intent=exit_intent(), event_time=1000)
    await first.get()  # Simulate loss of the process before submission/acknowledgement.
    restarted = ExecutionQueue()
    restarted.store = store
    await restarted.restore()
    job = await restarted.get()
    account = SimpleNamespace(
        get_order=lambda _id: None,
        has_unresolved_orders=lambda _symbol: False,
        has_pending_position_update=lambda _symbol: False,
        get_position=lambda _symbol: SimpleNamespace(side="LONG", quantity=Decimal("0.3")),
    )
    assert prepare_recovered_exit(job, account) is True
    assert job.intent.quantity == Decimal("0.3")
    await restarted.complete(job)
    assert await store.pending() == []


async def test_restart_expires_undispatched_entries_instead_of_replaying_old_signal(store):
    intent = replace(exit_intent(), side="BUY", reduce_only=False, client_order_id="entry-1")
    await store.save("entry", intent, 1000)
    queue = ExecutionQueue()
    queue.store = store
    await queue.restore()
    assert queue.qsize == 0
    assert await store.pending() == []
    assert not await store.save("entry", intent, 1000)
