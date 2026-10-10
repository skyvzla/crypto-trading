"""Durable intents for the existing account-leased execution worker."""

from dataclasses import asdict
from decimal import Decimal

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from trading_platform.shared.events import OrderIntent


class ExecutionJobStore:
    def __init__(self, pool: AsyncConnectionPool, account_id: str, strategy_id: str):
        self.pool, self.account_id, self.strategy_id = pool, account_id, strategy_id

    async def save(self, kind: str, intent: OrderIntent, event_time: int) -> bool:
        payload = asdict(intent)
        payload.update(price=str(intent.price), quantity=str(intent.quantity))
        async with self.pool.connection() as connection:
            row = await (await connection.execute(
                "INSERT INTO strategy_execution_jobs "
                "(account_id, strategy_id, client_order_id, kind, event_time, intent) "
                "VALUES (%s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (account_id, client_order_id) DO NOTHING RETURNING id",
                (self.account_id, self.strategy_id, intent.client_order_id, kind,
                 event_time, Jsonb(payload)),
            )).fetchone()
            if row is not None:
                return True
            existing = await (await connection.execute(
                "SELECT strategy_id, kind, intent, status, event_time FROM strategy_execution_jobs "
                "WHERE account_id = %s AND client_order_id = %s",
                (self.account_id, intent.client_order_id),
            )).fetchone()
        if existing is None or existing[:3] != (self.strategy_id, kind, payload) or existing[4] != event_time:
            raise ValueError("execution job identity conflicts with a persisted intent")
        return existing[3] == "pending"

    async def pending(self) -> list[tuple[str, int, OrderIntent]]:
        async with self.pool.connection() as connection:
            rows = await (await connection.execute(
                "SELECT kind, event_time, intent FROM strategy_execution_jobs "
                "WHERE account_id = %s AND strategy_id = %s AND status = 'pending' ORDER BY id",
                (self.account_id, self.strategy_id),
            )).fetchall()
        result = []
        for kind, event_time, payload in rows:
            payload.update(price=Decimal(payload["price"]), quantity=Decimal(payload["quantity"]))
            result.append((kind, event_time, OrderIntent(**payload)))
        return result

    async def finish(self, client_order_id: str, *, expired: bool = False) -> None:
        async with self.pool.connection() as connection:
            await connection.execute(
                "UPDATE strategy_execution_jobs SET status = %s, "
                "finished_at = COALESCE(finished_at, NOW()) "
                "WHERE account_id = %s AND strategy_id = %s AND client_order_id = %s",
                ("expired" if expired else "completed", self.account_id,
                 self.strategy_id, client_order_id),
            )
