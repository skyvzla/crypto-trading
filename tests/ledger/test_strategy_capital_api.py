"""Spike 资金状态只读 API 需要 LEDGER_TEST_DSN 指向 PostgreSQL。"""

from __future__ import annotations

import os
import urllib.parse
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from psycopg import sql

from trading_platform.ledger.api.routes import router
from trading_platform.ledger.db.migrations import apply_migrations
from trading_platform.ledger.db.models import LedgerDB, create_connection_pool
from trading_platform.strategies.spike.capital import CapitalPolicyConfig
from trading_platform.strategies.spike.capital_store import CapitalStore


pytestmark = pytest.mark.skipif(
    not os.getenv("LEDGER_TEST_DSN"),
    reason="LEDGER_TEST_DSN not set",
)


@pytest.fixture
async def ledger_and_capital_store():
    base_dsn = os.environ["LEDGER_TEST_DSN"]
    admin_pool = await create_connection_pool(base_dsn, 1, 2)
    schema = f"capital_api_{uuid4().hex}"
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
    pool = await create_connection_pool(dsn, 1, 4)
    await apply_migrations(pool, schema=schema)
    try:
        yield LedgerDB(pool), CapitalStore(pool)
    finally:
        await pool.close()
        cleanup_pool = await create_connection_pool(base_dsn, 1, 2)
        async with cleanup_pool.connection() as conn:
            await conn.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
            )
        await cleanup_pool.close()


@pytest.mark.asyncio
async def test_get_strategy_capital_status_returns_persisted_snapshot(
    ledger_and_capital_store,
):
    ledger, capital_store = ledger_and_capital_store
    account_id = f"capital-api-{uuid4().hex[:10]}"
    config = CapitalPolicyConfig(
        initial_account_capital="100",
        initial_trading_capital="50",
        profit_reinvest_ratio="0.5",
        minimum_trading_capital="10",
    )
    initialized = await capital_store.initialize(
        account_id=account_id,
        strategy_id="spike_short",
        config=config,
    )

    app = FastAPI()
    app.state.ledger_db = ledger
    app.include_router(router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get(
            "/api/v1/strategy-capital-status",
            params={"account_id": account_id, "strategy_id": "spike_short"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["account_id"] == account_id
    assert payload["strategy_id"] == "spike_short"
    assert Decimal(payload["account_capital"]) == Decimal("100")
    assert Decimal(payload["trading_capital"]) == Decimal("50")
    assert Decimal(payload["reserve_capital"]) == Decimal("50")
    assert Decimal(payload["minimum"]) == Decimal("10")
    assert Decimal(payload["profit_reinvest_ratio"]) == Decimal("0.5")
    assert payload["capital_breached"] is False
    assert payload["version"] == 1
    assert payload["updated_at"] == initialized.updated_at.isoformat().replace(
        "+00:00", "Z"
    )


@pytest.mark.asyncio
async def test_get_strategy_capital_status_returns_404_when_not_initialized(
    ledger_and_capital_store,
):
    ledger, _ = ledger_and_capital_store
    app = FastAPI()
    app.state.ledger_db = ledger
    app.include_router(router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get(
            "/api/v1/strategy-capital-status",
            params={"account_id": "missing", "strategy_id": "spike_short"},
        )

    assert response.status_code == 404
    assert response.json() == {"detail": "capital state not found"}
