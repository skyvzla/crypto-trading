import asyncio
import os
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from trading_platform.ledger.db.models import create_connection_pool
from trading_platform.shared.postgres_lease import (
    ExecutionLeaseUnavailableError,
    PostgresExecutionLease,
)


requires_postgres = pytest.mark.skipif(
    not os.getenv("LEDGER_TEST_DSN"),
    reason="LEDGER_TEST_DSN not set",
)


async def _try_acquire(lease: PostgresExecutionLease) -> bool:
    try:
        await lease.acquire()
    except ExecutionLeaseUnavailableError:
        return False
    return True


@pytest.mark.asyncio
@requires_postgres
async def test_only_one_concurrent_process_can_own_an_execution_account():
    dsn = os.environ["LEDGER_TEST_DSN"]
    pools = [
        await create_connection_pool(dsn, min_size=1, max_size=2)
        for _ in range(2)
    ]
    account_id = f"concurrent-{uuid4().hex}"
    leases = [PostgresExecutionLease(pool, account_id) for pool in pools]
    try:
        acquired = await asyncio.gather(*(_try_acquire(lease) for lease in leases))
        assert sorted(acquired) == [False, True]
    finally:
        await asyncio.gather(
            *(lease.release() for lease in leases), return_exceptions=True
        )
        await asyncio.gather(*(pool.close() for pool in pools))


@pytest.mark.asyncio
@requires_postgres
async def test_normal_release_allows_next_process_to_take_ownership():
    dsn = os.environ["LEDGER_TEST_DSN"]
    pools = [
        await create_connection_pool(dsn, min_size=1, max_size=2)
        for _ in range(2)
    ]
    account_id = f"release-{uuid4().hex}"
    first = PostgresExecutionLease(pools[0], account_id)
    second = PostgresExecutionLease(pools[1], account_id)
    try:
        await first.acquire()
        with pytest.raises(ExecutionLeaseUnavailableError):
            await second.acquire()
        await first.release()
        await second.acquire()
        assert second.held is True
    finally:
        await asyncio.gather(
            first.release(), second.release(), return_exceptions=True
        )
        await asyncio.gather(*(pool.close() for pool in pools))


@pytest.mark.asyncio
@requires_postgres
async def test_database_session_loss_releases_ownership_for_recovery_process():
    dsn = os.environ["LEDGER_TEST_DSN"]
    pools = [
        await create_connection_pool(dsn, min_size=1, max_size=2)
        for _ in range(2)
    ]
    account_id = f"abnormal-{uuid4().hex}"
    first = PostgresExecutionLease(pools[0], account_id)
    recovery = PostgresExecutionLease(pools[1], account_id)
    try:
        await first.acquire()
        assert first._connection is not None
        await first._connection.close()
        await recovery.acquire()
        assert recovery.held is True
    finally:
        await asyncio.gather(
            first.release(), recovery.release(), return_exceptions=True
        )
        await asyncio.gather(*(pool.close() for pool in pools))


@pytest.mark.asyncio
async def test_health_probe_timeout_marks_execution_lease_lost():
    blocker = asyncio.Event()

    async def blocked_probe(*args, **kwargs):
        await blocker.wait()

    lease = PostgresExecutionLease(Mock(), "timeout-account")
    lease._held = True
    lease._connection = Mock(execute=AsyncMock(side_effect=blocked_probe))

    failure = await lease.wait_lost(
        poll_interval_seconds=0,
        probe_timeout_seconds=0.01,
    )

    assert isinstance(failure, TimeoutError)
    assert lease.held is False
