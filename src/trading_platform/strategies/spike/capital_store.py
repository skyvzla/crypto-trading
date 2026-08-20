"""PostgreSQL persistence for account-scoped Spike trading capital."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from trading_platform.strategies.spike.capital import (
    CapitalPolicy,
    CapitalPolicyConfig,
    CapitalSettlement,
    CapitalState,
)


class CapitalStoreError(RuntimeError):
    """Persisted capital state is missing, conflicting, or otherwise unsafe."""


class CapitalConfigurationConflictError(CapitalStoreError):
    """An account/strategy already uses a different capital policy."""


class CapitalNotInitializedError(CapitalStoreError):
    """Settlement was requested before capital state was initialized."""


class CapitalSettlementConflictError(CapitalStoreError):
    """An idempotency key was reused for a different settlement fact."""


@dataclass(frozen=True, slots=True)
class CapitalSnapshot:
    account_id: str
    strategy_id: str
    config: CapitalPolicyConfig
    state: CapitalState
    version: int
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class CapitalSettlementResult:
    idempotency_key: str
    campaign_id: str | None
    occurred_at: datetime
    settlement: CapitalSettlement
    snapshot: CapitalSnapshot
    applied: bool


_STATE_COLUMNS = """
    account_id, strategy_id,
    initial_account_capital, initial_trading_capital,
    profit_reinvest_ratio, minimum_trading_capital,
    account_capital, trading_capital, reserve_capital,
    capital_breached, version, updated_at
"""
_INITIALIZATION_KEY = "capital:initialized:v1"


def _snapshot(row: dict[str, Any]) -> CapitalSnapshot:
    config = CapitalPolicyConfig(
        initial_account_capital=row["initial_account_capital"],
        initial_trading_capital=row["initial_trading_capital"],
        profit_reinvest_ratio=row["profit_reinvest_ratio"],
        minimum_trading_capital=row["minimum_trading_capital"],
    )
    return CapitalSnapshot(
        account_id=row["account_id"],
        strategy_id=row["strategy_id"],
        config=config,
        state=CapitalState(
            account_capital=row["account_capital"],
            trading_capital=row["trading_capital"],
            reserve_capital=row["reserve_capital"],
            capital_breached=row["capital_breached"],
        ),
        version=row["version"],
        updated_at=row["updated_at"],
    )


def _require_identity(value: str, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value


def _require_timestamp(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("occurred_at must be timezone-aware")
    return value


def _settlement_from_event(row: dict[str, Any]) -> CapitalSettlement:
    event_type = row["event_type"]
    before = CapitalState(
        account_capital=row["account_capital_before"],
        trading_capital=row["trading_capital_before"],
        reserve_capital=row["reserve_capital_before"],
        capital_breached=row["capital_breached_before"],
    )
    after = CapitalState(
        account_capital=row["account_capital_after"],
        trading_capital=row["trading_capital_after"],
        reserve_capital=row["reserve_capital_after"],
        capital_breached=row["capital_breached_after"],
    )
    return CapitalSettlement(
        net_pnl=row["net_pnl"],
        state_before=before,
        state_after=after,
        reinvested_profit=row["reinvested_profit"],
        reserve_change=row["reserve_capital_after"] - row["reserve_capital_before"],
        reserve_consumed=row["reserve_consumed"],
        event_type=event_type,
    )


class CapitalStore:
    """Atomically initialize, read, and settle account/strategy capital."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self.pool = pool

    @staticmethod
    async def _fetchone(
        conn: object, query: str, parameters: object = ()
    ) -> dict[str, Any] | None:
        cursor = conn.cursor(row_factory=dict_row)
        await cursor.execute(query, parameters)
        return await cursor.fetchone()

    async def get_state(
        self, *, account_id: str, strategy_id: str
    ) -> CapitalSnapshot | None:
        account_id = _require_identity(account_id, name="account_id")
        strategy_id = _require_identity(strategy_id, name="strategy_id")
        async with self.pool.connection() as conn:
            row = await self._fetchone(
                conn,
                f"SELECT {_STATE_COLUMNS} FROM strategy_capital_state "
                "WHERE account_id = %s AND strategy_id = %s",
                (account_id, strategy_id),
            )
        return _snapshot(row) if row is not None else None

    async def initialize(
        self,
        *,
        account_id: str,
        strategy_id: str,
        config: CapitalPolicyConfig,
    ) -> CapitalSnapshot:
        account_id = _require_identity(account_id, name="account_id")
        strategy_id = _require_identity(strategy_id, name="strategy_id")
        if not isinstance(config, CapitalPolicyConfig):
            raise TypeError("config must be CapitalPolicyConfig")
        initial = CapitalPolicy(config).initial_state()
        async with self.pool.connection() as conn:
            async with conn.transaction():
                row = await self._fetchone(
                    conn,
                    f"""
                    INSERT INTO strategy_capital_state (
                        account_id, strategy_id,
                        initial_account_capital, initial_trading_capital,
                        profit_reinvest_ratio, minimum_trading_capital,
                        account_capital, trading_capital, reserve_capital,
                        capital_breached
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                    )
                    ON CONFLICT (account_id, strategy_id) DO NOTHING
                    RETURNING {_STATE_COLUMNS}
                    """,
                    (
                        account_id,
                        strategy_id,
                        config.initial_account_capital,
                        config.initial_trading_capital,
                        config.profit_reinvest_ratio,
                        config.minimum_trading_capital,
                        initial.account_capital,
                        initial.trading_capital,
                        initial.reserve_capital,
                        initial.capital_breached,
                    ),
                )
                inserted = row is not None
                if row is None:
                    row = await self._fetchone(
                        conn,
                        f"SELECT {_STATE_COLUMNS} FROM strategy_capital_state "
                        "WHERE account_id = %s AND strategy_id = %s FOR UPDATE",
                        (account_id, strategy_id),
                    )
                if row is None:
                    raise CapitalStoreError("capital initialization produced no state")
                snapshot = _snapshot(row)
                if snapshot.config != config:
                    raise CapitalConfigurationConflictError(
                        "capital policy differs from the persisted configuration"
                    )
                if inserted:
                    now = datetime.now(UTC)
                    await conn.execute(
                        """
                        INSERT INTO strategy_capital_events (
                            id, account_id, strategy_id, idempotency_key,
                            event_type, net_pnl,
                            trading_capital_before, trading_capital_after,
                            reserve_capital_before, reserve_capital_after,
                            account_capital_before, account_capital_after,
                            reinvested_profit, reserve_consumed,
                            capital_breached_before, capital_breached_after,
                            occurred_at
                        ) VALUES (
                            %s, %s, %s, %s, 'INITIALIZED', 0,
                            %s, %s, %s, %s, %s, %s, 0, 0, FALSE, FALSE, %s
                        )
                        """,
                        (
                            uuid4(),
                            account_id,
                            strategy_id,
                            _INITIALIZATION_KEY,
                            initial.trading_capital,
                            initial.trading_capital,
                            initial.reserve_capital,
                            initial.reserve_capital,
                            initial.account_capital,
                            initial.account_capital,
                            now,
                        ),
                    )
                return snapshot

    async def settle(
        self,
        *,
        account_id: str,
        strategy_id: str,
        idempotency_key: str,
        net_pnl: Decimal | int | float | str,
        occurred_at: datetime,
        campaign_id: str | None = None,
    ) -> CapitalSettlementResult:
        account_id = _require_identity(account_id, name="account_id")
        strategy_id = _require_identity(strategy_id, name="strategy_id")
        idempotency_key = _require_identity(
            idempotency_key, name="idempotency_key"
        )
        occurred_at = _require_timestamp(occurred_at)
        if campaign_id is not None:
            campaign_id = _require_identity(campaign_id, name="campaign_id")

        async with self.pool.connection() as conn:
            async with conn.transaction():
                state_row = await self._fetchone(
                    conn,
                    f"SELECT {_STATE_COLUMNS} FROM strategy_capital_state "
                    "WHERE account_id = %s AND strategy_id = %s FOR UPDATE",
                    (account_id, strategy_id),
                )
                if state_row is None:
                    raise CapitalNotInitializedError(
                        "capital state must be initialized before settlement"
                    )
                current = _snapshot(state_row)
                policy = CapitalPolicy(current.config)
                # CapitalPolicy owns Decimal parsing and finiteness validation.
                settlement = policy.settle(current.state, net_pnl)
                requested_pnl = settlement.net_pnl
                event = await self._fetchone(
                    conn,
                    """
                    SELECT campaign_id, idempotency_key, event_type, net_pnl,
                           trading_capital_before, trading_capital_after,
                           reserve_capital_before, reserve_capital_after,
                           account_capital_before, account_capital_after,
                           reinvested_profit, reserve_consumed,
                           capital_breached_before, capital_breached_after,
                           occurred_at
                    FROM strategy_capital_events
                    WHERE account_id = %s AND strategy_id = %s
                      AND idempotency_key = %s
                    """,
                    (account_id, strategy_id, idempotency_key),
                )
                if event is not None:
                    if (
                        event["event_type"] == "INITIALIZED"
                        or event["net_pnl"] != requested_pnl
                        or event["campaign_id"] != campaign_id
                        or event["occurred_at"] != occurred_at
                    ):
                        raise CapitalSettlementConflictError(
                            "idempotency key belongs to a different settlement"
                        )
                    return CapitalSettlementResult(
                        idempotency_key=idempotency_key,
                        campaign_id=campaign_id,
                        occurred_at=event["occurred_at"],
                        settlement=_settlement_from_event(event),
                        snapshot=current,
                        applied=False,
                    )

                await conn.execute(
                    """
                    INSERT INTO strategy_capital_events (
                        id, account_id, strategy_id, campaign_id,
                        idempotency_key, event_type, net_pnl,
                        trading_capital_before, trading_capital_after,
                        reserve_capital_before, reserve_capital_after,
                        account_capital_before, account_capital_after,
                        reinvested_profit, reserve_consumed,
                        capital_breached_before, capital_breached_after,
                        occurred_at
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                    )
                    """,
                    (
                        uuid4(),
                        account_id,
                        strategy_id,
                        campaign_id,
                        idempotency_key,
                        settlement.event_type,
                        settlement.net_pnl,
                        settlement.state_before.trading_capital,
                        settlement.state_after.trading_capital,
                        settlement.state_before.reserve_capital,
                        settlement.state_after.reserve_capital,
                        settlement.state_before.account_capital,
                        settlement.state_after.account_capital,
                        settlement.reinvested_profit,
                        settlement.reserve_consumed,
                        settlement.state_before.capital_breached,
                        settlement.state_after.capital_breached,
                        occurred_at,
                    ),
                )
                updated = await self._fetchone(
                    conn,
                    f"""
                    UPDATE strategy_capital_state
                    SET account_capital = %s,
                        trading_capital = %s,
                        reserve_capital = %s,
                        capital_breached = %s,
                        version = version + 1,
                        updated_at = NOW()
                    WHERE account_id = %s AND strategy_id = %s
                    RETURNING {_STATE_COLUMNS}
                    """,
                    (
                        settlement.state_after.account_capital,
                        settlement.state_after.trading_capital,
                        settlement.state_after.reserve_capital,
                        settlement.state_after.capital_breached,
                        account_id,
                        strategy_id,
                    ),
                )
                if updated is None:
                    raise CapitalStoreError(
                        "capital state disappeared during settlement"
                    )
                persisted = _snapshot(updated)
                if persisted.state != settlement.state_after:
                    raise CapitalStoreError(
                        "capital settlement exceeds PostgreSQL numeric precision"
                    )
                return CapitalSettlementResult(
                    idempotency_key=idempotency_key,
                    campaign_id=campaign_id,
                    occurred_at=occurred_at,
                    settlement=settlement,
                    snapshot=persisted,
                    applied=True,
                )
