"""Shared safety primitives for manual testnet harnesses."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from trading_platform.ledger.db.models import create_connection_pool
from trading_platform.shared.config import DatabaseConfig
from trading_platform.shared.postgres_lease import PostgresExecutionLease


@asynccontextmanager
async def exclusive_testnet_account(account_id: str) -> AsyncIterator[None]:
    """Prevent a write harness from running beside Spike or another harness."""
    pool = await create_connection_pool(DatabaseConfig().dsn)
    lease = PostgresExecutionLease(pool, account_id)
    errors: list[BaseException] = []
    try:
        await lease.acquire()
        yield
    finally:
        try:
            await lease.release()
        except BaseException as exc:
            errors.append(exc)
        try:
            await pool.close()
        except BaseException as exc:
            errors.append(exc)
        if errors:
            raise BaseExceptionGroup("testnet execution lease cleanup failed", errors)
