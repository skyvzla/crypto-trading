"""PostgreSQL session locks used to enforce single-owner execution."""

from __future__ import annotations

import asyncio
from contextlib import AbstractAsyncContextManager
from typing import Any


class ExecutionLeaseUnavailableError(RuntimeError):
    """Raised when another process already owns an execution account."""


class PostgresExecutionLease:
    """Hold an account-scoped advisory lock on one dedicated DB session."""

    _TRY_LOCK = "SELECT pg_try_advisory_lock(hashtextextended(%s, 0))"
    _UNLOCK = "SELECT pg_advisory_unlock(hashtextextended(%s, 0))"

    def __init__(self, pool: Any, account_id: str):
        self._pool = pool
        self.account_id = account_id
        self.lock_name = f"trading-platform:execution-account:{account_id}"
        self._connection_context: AbstractAsyncContextManager[Any] | None = None
        self._connection: Any | None = None
        self._held = False
        self._released = False

    @property
    def held(self) -> bool:
        return self._held

    async def acquire(self) -> None:
        if self._held:
            return
        if self._released:
            raise RuntimeError("execution lease cannot be reacquired after release")

        context = self._pool.connection()
        connection = await context.__aenter__()
        self._connection_context = context
        self._connection = connection
        try:
            row = await (
                await connection.execute(self._TRY_LOCK, (self.lock_name,))
            ).fetchone()
            if row is None or row[0] is not True:
                raise ExecutionLeaseUnavailableError(
                    f"execution account is already owned: {self.account_id}"
                )
            # Session advisory locks survive COMMIT. End the implicit psycopg
            # transaction so this long-lived connection is never idle in one.
            await connection.commit()
        except BaseException:
            self._connection = None
            self._connection_context = None
            # The query may have acquired the server-side lock before the
            # client was cancelled or failed to read its result. Closing the
            # session makes that ambiguous state fail closed.
            try:
                await connection.close()
            finally:
                await context.__aexit__(None, None, None)
            raise
        self._held = True

    async def wait_lost(
        self,
        *,
        poll_interval_seconds: float = 1.0,
        probe_timeout_seconds: float = 2.0,
    ) -> BaseException:
        """Return only after the lock-holding database session is unusable."""
        if not self._held or self._connection is None:
            raise RuntimeError("execution lease is not held")
        while True:
            await asyncio.sleep(poll_interval_seconds)
            try:
                async with asyncio.timeout(probe_timeout_seconds):
                    await self._connection.execute("SELECT 1")
                    await self._connection.commit()
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                self._held = False
                return exc

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        connection = self._connection
        context = self._connection_context
        self._connection = None
        self._connection_context = None
        self._held = False
        error: BaseException | None = None
        if connection is not None:
            try:
                await connection.execute(self._UNLOCK, (self.lock_name,))
                await connection.commit()
            except BaseException as exc:
                # A failed explicit unlock must not return a possibly locked
                # session to the pool. Closing it releases advisory locks in
                # PostgreSQL even after a network or backend failure.
                try:
                    await connection.close()
                except BaseException as close_exc:
                    error = BaseExceptionGroup(
                        "failed to release execution lease", [exc, close_exc]
                    )
        if context is not None:
            try:
                await context.__aexit__(None, None, None)
            except BaseException as exc:
                if error is None:
                    error = exc
        if error is not None:
            raise error
