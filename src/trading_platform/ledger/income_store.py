"""Binance account-income facts persisted in PostgreSQL."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool


class IncomeFactConflictError(RuntimeError):
    """A Binance transaction identity was reused for different immutable facts."""


class IncomeStore:
    """Idempotently persist Binance income rows and aggregate funding fees."""

    _UPSERT = """
        INSERT INTO account_income_events (
            account_id, transaction_id, income_type, symbol, asset,
            amount, event_time, raw
        ) VALUES (
            %(account_id)s, %(transaction_id)s, %(income_type)s, %(symbol)s,
            %(asset)s, %(amount)s, %(event_time)s, %(raw)s
        )
        ON CONFLICT (account_id, income_type, transaction_id) DO UPDATE
        SET raw = account_income_events.raw
        WHERE account_income_events.symbol = EXCLUDED.symbol
          AND account_income_events.asset = EXCLUDED.asset
          AND account_income_events.amount = EXCLUDED.amount
          AND account_income_events.event_time = EXCLUDED.event_time
        RETURNING transaction_id
    """

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self.pool = pool

    async def upsert_income_history(
        self,
        *,
        account_id: str,
        rows: Sequence[Mapping[str, Any]],
    ) -> int:
        """Persist one Binance income-history page and return processed rows."""
        if not account_id.strip():
            raise ValueError("account_id must not be blank")
        parameters = [self._parse_row(account_id, row) for row in rows]
        if not parameters:
            return 0
        async with self.pool.connection() as connection:
            async with connection.transaction():
                async with connection.cursor() as cursor:
                    for parameter in parameters:
                        await cursor.execute(self._UPSERT, parameter)
                        if await cursor.fetchone() is None:
                            raise IncomeFactConflictError(
                                "income transaction identity belongs to a different fact"
                            )
        return len(parameters)

    async def funding_fee_total(
        self,
        *,
        account_id: str,
        symbol: str,
        start_at: datetime,
        end_at: datetime,
    ) -> Decimal:
        """Sum FUNDING_FEE rows for ``[start_at, end_at)``."""
        if not account_id.strip():
            raise ValueError("account_id must not be blank")
        if not symbol.strip():
            raise ValueError("symbol must not be blank")
        if start_at >= end_at:
            raise ValueError("start_at must be before end_at")
        async with self.pool.connection() as connection:
            row = await (
                await connection.execute(
                    "SELECT COALESCE(SUM(amount), 0) "
                    "FROM account_income_events "
                    "WHERE account_id = %s AND symbol = %s "
                    "AND income_type = 'FUNDING_FEE' "
                    "AND asset = 'USDT' "
                    "AND event_time >= %s AND event_time < %s",
                    (account_id, symbol, start_at, end_at),
                )
            ).fetchone()
        return Decimal(row[0])

    @staticmethod
    def _parse_row(account_id: str, row: Mapping[str, Any]) -> dict[str, Any]:
        try:
            transaction_id = int(row["tranId"])
            timestamp_ms = int(row["time"])
            amount = Decimal(str(row["income"]))
            income_type = str(row["incomeType"])
            asset = str(row["asset"])
        except (KeyError, TypeError, ValueError, InvalidOperation) as error:
            raise ValueError("invalid Binance income row") from error
        if transaction_id < 0 or timestamp_ms < 0 or not amount.is_finite():
            raise ValueError("invalid Binance income row")
        if not income_type.strip() or not asset.strip():
            raise ValueError("invalid Binance income row")
        event_time = datetime.fromtimestamp(timestamp_ms // 1000, tz=UTC) + timedelta(
            milliseconds=timestamp_ms % 1000
        )
        return {
            "account_id": account_id,
            "transaction_id": transaction_id,
            "income_type": income_type,
            "symbol": str(row.get("symbol") or ""),
            "asset": asset,
            "amount": amount,
            "event_time": event_time,
            "raw": Jsonb(dict(row)),
        }
