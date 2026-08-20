"""CapitalStore integration tests against a real isolated PostgreSQL schema."""

from __future__ import annotations

import asyncio
import os
import urllib.parse
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from psycopg import sql

from trading_platform.ledger.db.migrations import apply_migrations
from trading_platform.ledger.db.models import create_connection_pool
from trading_platform.strategies.spike.capital import (
    CapitalPolicy,
    CapitalPolicyConfig,
)
from trading_platform.strategies.spike.capital_store import (
    CapitalConfigurationConflictError,
    CapitalNotInitializedError,
    CapitalSettlementConflictError,
    CapitalStore,
    CapitalStoreError,
)


pytestmark = pytest.mark.skipif(
    not os.getenv("LEDGER_TEST_DSN"), reason="LEDGER_TEST_DSN not set"
)


@pytest.fixture
async def capital_store():
    base_dsn = os.environ["LEDGER_TEST_DSN"]
    admin_pool = await create_connection_pool(base_dsn, 1, 2)
    schema = f"capital_{uuid4().hex}"
    async with admin_pool.connection() as conn:
        await conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    await admin_pool.close()

    separator = "&" if "?" in base_dsn else "?"
    dsn = (
        base_dsn
        + separator
        + "options="
        + urllib.parse.quote(f"-csearch_path={schema}")
    )
    pool = await create_connection_pool(dsn, 1, 6)
    await apply_migrations(pool, schema=schema)
    try:
        yield CapitalStore(pool)
    finally:
        await pool.close()
        cleanup = await create_connection_pool(base_dsn, 1, 2)
        async with cleanup.connection() as conn:
            await conn.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
            )
        await cleanup.close()


@pytest.mark.asyncio
async def test_initialize_is_idempotent_and_state_can_be_read(capital_store):
    config = CapitalPolicyConfig(
        initial_account_capital="100",
        initial_trading_capital="50",
        profit_reinvest_ratio="0.5",
        minimum_trading_capital="10",
    )

    first = await capital_store.initialize(
        account_id="account-a",
        strategy_id="spike-short",
        config=config,
    )
    second = await capital_store.initialize(
        account_id="account-a",
        strategy_id="spike-short",
        config=config,
    )

    assert first == second
    assert await capital_store.get_state(
        account_id="account-a", strategy_id="spike-short"
    ) == first
    assert first.config == config
    assert first.state == CapitalPolicy(config).initial_state()
    assert first.version == 1
    assert isinstance(first.state.trading_capital, Decimal)
    async with capital_store.pool.connection() as conn:
        row = await (
            await conn.execute(
                """
                SELECT COUNT(*), MIN(event_type), MIN(net_pnl)
                FROM strategy_capital_events
                WHERE account_id = %s AND strategy_id = %s
                """,
                ("account-a", "spike-short"),
            )
        ).fetchone()
    assert row == (1, "INITIALIZED", Decimal("0"))


@pytest.mark.asyncio
async def test_profit_settlement_is_atomic_and_idempotent(capital_store):
    config = CapitalPolicyConfig(
        initial_account_capital="100",
        initial_trading_capital="50",
        profit_reinvest_ratio="0.5",
        minimum_trading_capital="10",
    )
    await capital_store.initialize(
        account_id="account-profit",
        strategy_id="spike-short",
        config=config,
    )
    occurred_at = datetime(2026, 8, 20, 8, 30, tzinfo=UTC)

    first = await capital_store.settle(
        account_id="account-profit",
        strategy_id="spike-short",
        idempotency_key="campaign-1:closed",
        campaign_id="campaign-1",
        net_pnl=Decimal("20"),
        occurred_at=occurred_at,
    )
    duplicate = await capital_store.settle(
        account_id="account-profit",
        strategy_id="spike-short",
        idempotency_key="campaign-1:closed",
        campaign_id="campaign-1",
        net_pnl=Decimal("20"),
        occurred_at=occurred_at,
    )

    assert first.applied is True
    assert duplicate.applied is False
    assert duplicate.settlement == first.settlement
    assert duplicate.snapshot == first.snapshot
    assert first.occurred_at == occurred_at
    assert first.settlement.event_type == "PROFIT_SETTLED"
    assert first.settlement.reinvested_profit == Decimal("10")
    assert first.settlement.reserve_change == Decimal("10")
    assert first.snapshot.state.account_capital == Decimal("120")
    assert first.snapshot.state.trading_capital == Decimal("60")
    assert first.snapshot.state.reserve_capital == Decimal("60")
    assert first.snapshot.version == 2
    assert await capital_store.get_state(
        account_id="account-profit", strategy_id="spike-short"
    ) == first.snapshot
    async with capital_store.pool.connection() as conn:
        event = await (
            await conn.execute(
                """
                SELECT event_type, net_pnl,
                       trading_capital_before, trading_capital_after,
                       reserve_capital_before, reserve_capital_after,
                       reinvested_profit, reserve_consumed
                FROM strategy_capital_events
                WHERE account_id = %s AND strategy_id = %s
                  AND idempotency_key = %s
                """,
                ("account-profit", "spike-short", "campaign-1:closed"),
            )
        ).fetchone()
    assert event == (
        "PROFIT_SETTLED",
        Decimal("20"),
        Decimal("50"),
        Decimal("60"),
        Decimal("50"),
        Decimal("60"),
        Decimal("10"),
        Decimal("0"),
    )


@pytest.mark.asyncio
async def test_concurrent_duplicate_settlement_is_applied_once(capital_store):
    config = CapitalPolicyConfig(
        initial_account_capital="100",
        initial_trading_capital="50",
        profit_reinvest_ratio="0.5",
        minimum_trading_capital="10",
    )
    await capital_store.initialize(
        account_id="account-concurrent",
        strategy_id="spike-short",
        config=config,
    )
    occurred_at = datetime(2026, 8, 20, 9, 0, tzinfo=UTC)

    results = await asyncio.gather(
        *(
            capital_store.settle(
                account_id="account-concurrent",
                strategy_id="spike-short",
                idempotency_key="campaign-concurrent:closed",
                campaign_id="campaign-concurrent",
                net_pnl="-15",
                occurred_at=occurred_at,
            )
            for _ in range(20)
        )
    )

    assert sum(result.applied for result in results) == 1
    snapshot = await capital_store.get_state(
        account_id="account-concurrent", strategy_id="spike-short"
    )
    assert snapshot is not None
    assert snapshot.version == 2
    assert snapshot.state.account_capital == Decimal("85")
    assert snapshot.state.trading_capital == Decimal("35")
    assert snapshot.state.reserve_capital == Decimal("50")
    async with capital_store.pool.connection() as conn:
        row = await (
            await conn.execute(
                """
                SELECT COUNT(*)
                FROM strategy_capital_events
                WHERE account_id = %s AND strategy_id = %s
                  AND idempotency_key = %s
                """,
                (
                    "account-concurrent",
                    "spike-short",
                    "campaign-concurrent:closed",
                ),
            )
        ).fetchone()
    assert row == (1,)


@pytest.mark.asyncio
async def test_reused_idempotency_key_with_different_fact_fails_closed(capital_store):
    config = CapitalPolicyConfig(
        initial_account_capital="100",
        initial_trading_capital="50",
        profit_reinvest_ratio="0.5",
        minimum_trading_capital="10",
    )
    await capital_store.initialize(
        account_id="account-conflict",
        strategy_id="spike-short",
        config=config,
    )
    occurred_at = datetime(2026, 8, 20, 9, 30, tzinfo=UTC)
    applied = await capital_store.settle(
        account_id="account-conflict",
        strategy_id="spike-short",
        idempotency_key="campaign-conflict:closed",
        campaign_id="campaign-conflict",
        net_pnl="10",
        occurred_at=occurred_at,
    )

    with pytest.raises(CapitalSettlementConflictError):
        await capital_store.settle(
            account_id="account-conflict",
            strategy_id="spike-short",
            idempotency_key="campaign-conflict:closed",
            campaign_id="campaign-conflict",
            net_pnl="-10",
            occurred_at=occurred_at,
        )

    assert await capital_store.get_state(
        account_id="account-conflict", strategy_id="spike-short"
    ) == applied.snapshot


@pytest.mark.asyncio
async def test_historical_retry_preserves_original_breach_facts(capital_store):
    config = CapitalPolicyConfig("100", "50", "0.5", "10")
    await capital_store.initialize(
        account_id="account-historical-retry",
        strategy_id="spike_short",
        config=config,
    )
    first_time = datetime(2026, 8, 20, 9, 45, tzinfo=UTC)
    first = await capital_store.settle(
        account_id="account-historical-retry",
        strategy_id="spike_short",
        idempotency_key="campaign-first:closed",
        campaign_id="campaign-first",
        net_pnl="-10",
        occurred_at=first_time,
    )
    await capital_store.settle(
        account_id="account-historical-retry",
        strategy_id="spike_short",
        idempotency_key="campaign-breach:closed",
        campaign_id="campaign-breach",
        net_pnl="-100",
        occurred_at=datetime(2026, 8, 20, 9, 50, tzinfo=UTC),
    )

    replay = await capital_store.settle(
        account_id="account-historical-retry",
        strategy_id="spike_short",
        idempotency_key="campaign-first:closed",
        campaign_id="campaign-first",
        net_pnl="-10",
        occurred_at=first_time,
    )

    assert replay.applied is False
    assert replay.settlement == first.settlement
    assert replay.snapshot.state.capital_breached is True


@pytest.mark.asyncio
async def test_reused_idempotency_key_with_different_time_fails_closed(capital_store):
    await capital_store.initialize(
        account_id="account-time-conflict",
        strategy_id="spike_short",
        config=CapitalPolicyConfig("100", "50", "0.5", "10"),
    )
    occurred_at = datetime(2026, 8, 20, 9, 55, tzinfo=UTC)
    await capital_store.settle(
        account_id="account-time-conflict",
        strategy_id="spike_short",
        idempotency_key="campaign-time:closed",
        campaign_id="campaign-time",
        net_pnl="1",
        occurred_at=occurred_at,
    )

    with pytest.raises(CapitalSettlementConflictError):
        await capital_store.settle(
            account_id="account-time-conflict",
            strategy_id="spike_short",
            idempotency_key="campaign-time:closed",
            campaign_id="campaign-time",
            net_pnl="1",
            occurred_at=occurred_at + timedelta(milliseconds=1),
        )

@pytest.mark.asyncio
async def test_initialize_rejects_configuration_drift(capital_store):
    original = CapitalPolicyConfig(
        initial_account_capital="100",
        initial_trading_capital="50",
        profit_reinvest_ratio="0.5",
        minimum_trading_capital="10",
    )
    initialized = await capital_store.initialize(
        account_id="account-config",
        strategy_id="spike-short",
        config=original,
    )

    with pytest.raises(CapitalConfigurationConflictError):
        await capital_store.initialize(
            account_id="account-config",
            strategy_id="spike-short",
            config=CapitalPolicyConfig(
                initial_account_capital="100",
                initial_trading_capital="60",
                profit_reinvest_ratio="0.5",
                minimum_trading_capital="10",
            ),
        )

    assert await capital_store.get_state(
        account_id="account-config", strategy_id="spike-short"
    ) == initialized


@pytest.mark.asyncio
async def test_loss_beyond_trading_pool_records_reserve_consumption(capital_store):
    await capital_store.initialize(
        account_id="account-breach",
        strategy_id="spike-short",
        config=CapitalPolicyConfig(
            initial_account_capital="100",
            initial_trading_capital="50",
            profit_reinvest_ratio="0.5",
            minimum_trading_capital="10",
        ),
    )

    result = await capital_store.settle(
        account_id="account-breach",
        strategy_id="spike-short",
        idempotency_key="campaign-breach:closed",
        campaign_id="campaign-breach",
        net_pnl="-70",
        occurred_at=datetime(2026, 8, 20, 10, 0, tzinfo=UTC),
    )

    assert result.settlement.event_type == "CAPITAL_BREACH"
    assert result.settlement.reserve_consumed == Decimal("20")
    assert result.snapshot.state.account_capital == Decimal("30")
    assert result.snapshot.state.trading_capital == Decimal("0")
    assert result.snapshot.state.reserve_capital == Decimal("30")
    assert result.snapshot.state.capital_breached is True


@pytest.mark.asyncio
async def test_unrepresentable_settlement_rolls_back_instead_of_rounding(capital_store):
    initialized = await capital_store.initialize(
        account_id="account-precision",
        strategy_id="spike-short",
        config=CapitalPolicyConfig(
            initial_account_capital="100",
            initial_trading_capital="50",
            profit_reinvest_ratio="0.5",
            minimum_trading_capital="10",
        ),
    )

    with pytest.raises(CapitalStoreError, match="precision"):
        await capital_store.settle(
            account_id="account-precision",
            strategy_id="spike-short",
            idempotency_key="campaign-precision:closed",
            campaign_id="campaign-precision",
            net_pnl="0.0000000000000000001",
            occurred_at=datetime(2026, 8, 20, 10, 30, tzinfo=UTC),
        )

    assert await capital_store.get_state(
        account_id="account-precision", strategy_id="spike-short"
    ) == initialized
    async with capital_store.pool.connection() as conn:
        row = await (
            await conn.execute(
                """
                SELECT COUNT(*)
                FROM strategy_capital_events
                WHERE account_id = %s AND strategy_id = %s
                  AND idempotency_key = %s
                """,
                (
                    "account-precision",
                    "spike-short",
                    "campaign-precision:closed",
                ),
            )
        ).fetchone()
    assert row == (0,)


@pytest.mark.asyncio
async def test_concurrent_distinct_settlements_do_not_lose_an_update(capital_store):
    await capital_store.initialize(
        account_id="account-two-events",
        strategy_id="spike-short",
        config=CapitalPolicyConfig(
            initial_account_capital="100",
            initial_trading_capital="50",
            profit_reinvest_ratio="0.5",
            minimum_trading_capital="10",
        ),
    )
    occurred_at = datetime(2026, 8, 20, 11, 0, tzinfo=UTC)

    results = await asyncio.gather(
        capital_store.settle(
            account_id="account-two-events",
            strategy_id="spike-short",
            idempotency_key="campaign-a:closed",
            campaign_id="campaign-a",
            net_pnl="-5",
            occurred_at=occurred_at,
        ),
        capital_store.settle(
            account_id="account-two-events",
            strategy_id="spike-short",
            idempotency_key="campaign-b:closed",
            campaign_id="campaign-b",
            net_pnl="-7",
            occurred_at=occurred_at,
        ),
    )

    assert all(result.applied for result in results)
    snapshot = await capital_store.get_state(
        account_id="account-two-events", strategy_id="spike-short"
    )
    assert snapshot is not None
    assert snapshot.version == 3
    assert snapshot.state.account_capital == Decimal("88")
    assert snapshot.state.trading_capital == Decimal("38")
    assert snapshot.state.reserve_capital == Decimal("50")


@pytest.mark.asyncio
async def test_settlement_without_initialized_state_fails_closed(capital_store):
    with pytest.raises(CapitalNotInitializedError):
        await capital_store.settle(
            account_id="account-missing",
            strategy_id="spike-short",
            idempotency_key="campaign-missing:closed",
            campaign_id="campaign-missing",
            net_pnl="10",
            occurred_at=datetime(2026, 8, 20, 11, 30, tzinfo=UTC),
        )

    assert await capital_store.get_state(
        account_id="account-missing", strategy_id="spike-short"
    ) is None
