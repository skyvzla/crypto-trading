"""需要 LEDGER_TEST_DSN 指向可创建临时 schema 的真实 PostgreSQL。"""

import asyncio
import os
import shutil
from pathlib import Path
from uuid import uuid4

import pytest
from psycopg import sql
from psycopg.errors import UndefinedColumn

from trading_platform.ledger.db.migrations import (
    MIGRATIONS_DIR,
    MigrationError,
    apply_migrations,
    load_migrations,
    verify_current,
)
from trading_platform.ledger.db.models import create_connection_pool


pytestmark = pytest.mark.skipif(
    not os.getenv("LEDGER_TEST_DSN"),
    reason="LEDGER_TEST_DSN not set",
)


@pytest.fixture
async def migration_db():
    pool = await create_connection_pool(os.environ["LEDGER_TEST_DSN"], 1, 4)
    schema = f"ledger_migration_{uuid4().hex}"
    async with pool.connection() as conn:
        await conn.execute(
            sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema))
        )
    try:
        yield pool, schema
    finally:
        async with pool.connection() as conn:
            await conn.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
            )
        await pool.close()


@pytest.mark.asyncio
async def test_fresh_database_migrates_and_second_run_is_idempotent(migration_db):
    pool, schema = migration_db

    with pytest.raises(MigrationError, match="has not been migrated"):
        await verify_current(pool, schema=schema)

    first = await apply_migrations(pool, schema=schema)
    second = await apply_migrations(pool, schema=schema)

    current_version = len(load_migrations())
    assert first.current_version == current_version
    assert first.applied_versions == tuple(range(1, current_version + 1))
    assert second.current_version == current_version
    assert second.applied_versions == ()
    assert await verify_current(pool, schema=schema) == current_version

    async with pool.connection() as conn:
        tables = await (
            await conn.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = %s AND table_name IN "
                "('strategy_capital_state', 'strategy_capital_events', "
                "'account_income_events') "
                "ORDER BY table_name",
                (schema,),
            )
        ).fetchall()
    assert tables == [
        ("account_income_events",),
        ("strategy_capital_events",),
        ("strategy_capital_state",),
    ]

    async with pool.connection() as conn:
        indexes = await (
            await conn.execute(
                "SELECT indexdef FROM pg_indexes "
                "WHERE schemaname = %s "
                "AND indexname = "
                "'idx_account_income_events_account_symbol_time'",
                (schema,),
            )
        ).fetchall()
    assert len(indexes) == 1
    assert "account_id, symbol, event_time DESC" in indexes[0][0]


@pytest.mark.asyncio
async def test_existing_schema_is_adopted_without_losing_rows(migration_db):
    pool, schema = migration_db
    initial = load_migrations()[0]
    async with pool.connection() as conn:
        async with conn.transaction():
            await conn.execute(
                sql.SQL("SET LOCAL search_path TO {}, pg_catalog").format(
                    sql.Identifier(schema)
                )
            )
            await conn.execute(initial.sql)
            await conn.execute(
                "INSERT INTO orders ("
                "account_id, strategy_id, symbol, order_id, client_order_id, "
                "side, order_type, quantity, status"
                ") VALUES ('existing', 'spike_short', 'AKEUSDT', '1', "
                "'existing-order', 'SELL', 'LIMIT', 1, 'NEW')"
            )

    result = await apply_migrations(pool, schema=schema)

    async with pool.connection() as conn:
        count = await (
            await conn.execute(
                sql.SQL("SELECT COUNT(*) FROM {}.orders WHERE account_id = %s").format(
                    sql.Identifier(schema)
                ),
                ("existing",),
            )
        ).fetchone()
    current_version = len(load_migrations())
    assert result.applied_versions == tuple(range(1, current_version + 1))
    assert count == (1,)


@pytest.mark.asyncio
async def test_capital_breach_facts_are_backfilled_when_upgrading_from_0011(
    migration_db, tmp_path
):
    pool, schema = migration_db
    old_migrations = tmp_path / "migrations"
    old_migrations.mkdir()
    for source in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if source.name.startswith("0012_"):
            continue
        shutil.copy(source, old_migrations / source.name)
    await apply_migrations(pool, schema=schema, directory=old_migrations)

    async with pool.connection() as conn:
        async with conn.transaction():
            await conn.execute(
                sql.SQL("SET LOCAL search_path TO {}, pg_catalog").format(
                    sql.Identifier(schema)
                )
            )
            for index, values in enumerate(
                (
                    ("INITIALIZED", "0", "50", "50", "50", "50", "100", "100"),
                    ("CAPITAL_BREACH", "-110", "50", "0", "50", "-10", "100", "-10"),
                    ("PROFIT_SETTLED", "2", "0", "1", "-10", "-9", "-10", "-8"),
                ),
                start=1,
            ):
                await conn.execute(
                    """
                    INSERT INTO strategy_capital_events (
                        id, account_id, strategy_id, idempotency_key,
                        event_type, net_pnl,
                        trading_capital_before, trading_capital_after,
                        reserve_capital_before, reserve_capital_after,
                        account_capital_before, account_capital_after,
                        reinvested_profit, reserve_consumed,
                        occurred_at, created_at
                    ) VALUES (
                        %s, 'upgrade-account', 'spike_short', %s,
                        %s, %s, %s, %s, %s, %s, %s, %s,
                        0, 0, %s, %s
                    )
                    """,
                    (
                        uuid4(),
                        f"upgrade-{index}",
                        *values,
                        f"2026-08-20 00:0{index}:00+00",
                        f"2026-08-20 00:0{index}:00+00",
                    ),
                )

    result = await apply_migrations(pool, schema=schema)

    assert result.applied_versions == (12,)
    async with pool.connection() as conn:
        async with conn.transaction():
            await conn.execute(
                sql.SQL("SET LOCAL search_path TO {}, pg_catalog").format(
                    sql.Identifier(schema)
                )
            )
            facts = await (
                await conn.execute(
                    """
                    SELECT capital_breached_before, capital_breached_after
                    FROM strategy_capital_events
                    WHERE account_id = 'upgrade-account'
                    ORDER BY created_at
                    """
                )
            ).fetchall()
    assert facts == [(False, False), (False, True), (True, True)]


@pytest.mark.asyncio
async def test_concurrent_runners_apply_each_version_once(migration_db):
    pool, schema = migration_db

    first, second = await asyncio.gather(
        apply_migrations(pool, schema=schema),
        apply_migrations(pool, schema=schema),
    )

    assert sorted((first.applied_versions, second.applied_versions)) == [
        (),
        tuple(range(1, len(load_migrations()) + 1)),
    ]
    async with pool.connection() as conn:
        row = await (
            await conn.execute(
                sql.SQL(
                    "SELECT COUNT(*), MIN(version), MAX(version) "
                    "FROM {}.ledger_schema_migrations"
                ).format(sql.Identifier(schema))
            )
        ).fetchone()
    current_version = len(load_migrations())
    assert row == (current_version, 1, current_version)

@pytest.mark.asyncio
async def test_web_performance_indexes_are_migrated(migration_db):
    pool, schema = migration_db
    await apply_migrations(pool, schema=schema)

    async with pool.connection() as conn:
        rows = await (
            await conn.execute(
                "SELECT indexname, indexdef FROM pg_indexes "
                "WHERE schemaname = %s AND indexname IN "
                "('idx_trades_account_exchange_time', "
                "'idx_trades_campaign_performance') ORDER BY indexname",
                (schema,),
            )
        ).fetchall()

    assert [row[0] for row in rows] == [
        "idx_trades_account_exchange_time",
        "idx_trades_campaign_performance",
    ]
    definitions = {name: definition for name, definition in rows}
    assert "account_id, exchange_time DESC" in definitions[
        "idx_trades_account_exchange_time"
    ]
    assert "account_id, strategy_id, symbol, exchange_time DESC" in definitions[
        "idx_trades_campaign_performance"
    ]
    assert "WHERE (campaign_id IS NOT NULL)" in definitions[
        "idx_trades_campaign_performance"
    ]


@pytest.mark.asyncio
async def test_changed_applied_migration_is_rejected(migration_db):
    pool, schema = migration_db
    await apply_migrations(pool, schema=schema)
    async with pool.connection() as conn:
        await conn.execute(
            sql.SQL(
                "UPDATE {}.ledger_schema_migrations SET checksum = %s "
                "WHERE version = 1"
            ).format(sql.Identifier(schema)),
            ("0" * 64,),
        )

    with pytest.raises(MigrationError, match="does not match this build"):
        await verify_current(pool, schema=schema)
    with pytest.raises(MigrationError, match="does not match this build"):
        await apply_migrations(pool, schema=schema)


@pytest.mark.asyncio
async def test_pending_migrations_roll_back_as_one_transaction(
    migration_db, tmp_path: Path
):
    pool, schema = migration_db
    (tmp_path / "0001_first.sql").write_text(
        "CREATE TABLE transaction_probe (id INTEGER PRIMARY KEY);\n"
    )
    (tmp_path / "0002_broken.sql").write_text(
        "CREATE TABLE must_rollback (id INTEGER);\nSELECT missing_column;\n"
    )

    with pytest.raises(UndefinedColumn, match="missing_column"):
        await apply_migrations(pool, schema=schema, directory=tmp_path)

    async with pool.connection() as conn:
        rows = await (
            await conn.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = %s AND table_name IN "
                "('ledger_schema_migrations', 'transaction_probe', 'must_rollback')",
                (schema,),
            )
        ).fetchall()
    assert rows == []


def test_migration_files_must_be_contiguous(tmp_path: Path):
    (tmp_path / "0001_first.sql").write_text("SELECT 1;\n")
    (tmp_path / "0003_gap.sql").write_text("SELECT 1;\n")

    with pytest.raises(MigrationError, match="contiguous"):
        load_migrations(tmp_path)
