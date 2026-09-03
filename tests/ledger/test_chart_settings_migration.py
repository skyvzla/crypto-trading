"""Chart settings migration contract tests."""

from __future__ import annotations

import os
from uuid import uuid4

import pytest
from psycopg import sql

from trading_platform.ledger.db.migrations import apply_migrations, load_migrations
from trading_platform.ledger.db.models import create_connection_pool


pytestmark = pytest.mark.skipif(
    not os.getenv("LEDGER_TEST_DSN"),
    reason="LEDGER_TEST_DSN not set",
)


@pytest.mark.asyncio
async def test_chart_settings_migration_is_singleton_and_idempotent():
    pool = await create_connection_pool(os.environ["LEDGER_TEST_DSN"], 1, 3)
    schema = f"chart_migration_{uuid4().hex}"
    try:
        async with pool.connection() as conn:
            await conn.execute(
                sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema))
            )

        first = await apply_migrations(pool, schema=schema)
        second = await apply_migrations(pool, schema=schema)
        assert first.applied_versions[-1] == len(load_migrations())
        assert second.applied_versions == ()

        async with pool.connection() as conn:
            row = await (
                await conn.execute(
                    sql.SQL(
                        "SELECT COUNT(*), setting_key, jsonb_typeof(settings) "
                        "FROM {}.chart_settings GROUP BY setting_key, settings"
                    ).format(sql.Identifier(schema))
                )
            ).fetchone()
        assert row == (1, "global", "object")
    finally:
        async with pool.connection() as conn:
            await conn.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
            )
        await pool.close()
