"""需要 LEDGER_TEST_DSN 指向可清理的真实 PostgreSQL。"""

import os
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from trading_platform.ledger.api.routes import router
from trading_platform.ledger.db.models import (
    LedgerDB,
    Order,
    Position,
    Trade,
    create_connection_pool,
)

pytestmark = pytest.mark.skipif(
    not os.getenv("LEDGER_TEST_DSN"),
    reason="LEDGER_TEST_DSN not set",
)


@pytest.fixture
async def ledger():
    pool = await create_connection_pool(os.environ["LEDGER_TEST_DSN"], 1, 4)
    schema = Path("src/trading_platform/ledger/db/schema.sql").read_text()
    async with pool.connection() as conn:
        await conn.execute(schema)
    yield LedgerDB(pool)
    await pool.close()


@pytest.fixture
async def client(ledger):
    app = FastAPI()
    app.state.ledger_db = ledger
    app.include_router(router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as value:
        yield value


@pytest.mark.asyncio
async def test_health_pagination_idempotency_and_pnl(ledger, client):
    suffix = uuid4().hex[:10]
    account = f"a-{suffix}"
    now = datetime.now(timezone.utc)
    order = Order(
        account_id=account,
        strategy_id="s",
        symbol="BTCUSDT",
        order_id="o1",
        client_order_id=f"c-{suffix}",
        side="BUY",
        order_type="LIMIT",
        quantity=Decimal("2"),
        price=Decimal("100"),
        status="NEW",
        exchange_created_at=now,
    )
    first = await ledger.insert_order(order)
    order.status = "FILLED"
    second = await ledger.insert_order(order)
    assert first == second

    trade = Trade(
        account_id=account,
        strategy_id="s",
        symbol="BTCUSDT",
        trade_id="t1",
        order_id="o1",
        client_order_id=f"c-{suffix}",
        side="BUY",
        quantity=Decimal("2"),
        price=Decimal("100"),
        quote_quantity=Decimal("200"),
        commission=Decimal("1"),
        commission_asset="USDT",
        realized_pnl=Decimal("5"),
        exchange_time=now,
    )
    assert await ledger.insert_trade(trade) > 0
    assert await ledger.insert_trade(trade) == 0

    await ledger.upsert_position(
        Position(
            account_id=account,
            strategy_id="s",
            symbol="BTCUSDT",
            position_side="LONG",
            quantity=Decimal("2"),
            entry_price=Decimal("100"),
            unrealized_pnl=Decimal("3"),
        )
    )

    assert (await client.get("/api/v1/health")).status_code == 200
    orders = (
        await client.get(
            "/api/v1/orders",
            params={"account_id": account, "limit": 1},
        )
    ).json()
    assert orders["total"] == 1
    assert orders["limit"] == 1
    assert len(orders["items"]) == 1

    positions = (
        await client.get(
            "/api/v1/positions",
            params={"account_id": account, "limit": 1},
        )
    ).json()
    assert positions["total"] == 1

    pnl = (
        await client.get("/api/v1/pnl", params={"account_id": account})
    ).json()
    assert pnl["total_trades"] == 1
    assert Decimal(pnl["net_pnl"]) == Decimal("7")


@pytest.mark.asyncio
async def test_subcategory_optimistic_concurrency_and_audit(client, ledger):
    name = f"spike-{uuid4().hex[:10]}"
    # 未配置项必须按关闭处理，避免策略在账本不可知时新增风险。
    assert (await client.get(f"/api/v1/subcategory-admissions/{name}")).status_code == 404
    assert await ledger.is_subcategory_enabled(name) is False
    created = await client.put(
        f"/api/v1/subcategory-admissions/{name}",
        json={
            "enabled": True,
            "expected_version": 0,
            "updated_by": "tester",
            "reason": "open",
        },
    )
    assert created.status_code == 200
    assert created.json()["version"] == 1
    assert await ledger.is_subcategory_enabled(name) is True

    updated = await client.put(
        f"/api/v1/subcategory-admissions/{name}",
        json={
            "enabled": False,
            "expected_version": 1,
            "updated_by": "tester",
            "reason": "close",
        },
    )
    assert updated.status_code == 200
    assert updated.json()["version"] == 2
    assert updated.json()["enabled"] is False

    admission = await client.get(f"/api/v1/subcategory-admissions/{name}")
    assert admission.status_code == 200
    assert admission.json()["enabled"] is False
    assert await ledger.is_subcategory_enabled(name) is False

    stale = await client.put(
        f"/api/v1/subcategory-admissions/{name}",
        json={
            "enabled": True,
            "expected_version": 1,
            "updated_by": "tester",
        },
    )
    assert stale.status_code == 409

    audit = (
        await client.get(
            "/api/v1/subcategory-admission-audit",
            params={"subcategory": name},
        )
    ).json()
    assert audit["total"] == 2
    changes = [
        (item["previous_enabled"], item["enabled"], item["version"])
        for item in reversed(audit["items"])
    ]
    assert changes == [(None, True, 1), (True, False, 2)]


@pytest.mark.asyncio
async def test_unconfirmed_controls_are_not_exposed(client):
    assert (
        await client.get("/api/v1/account_control_state/account_a")
    ).status_code == 404
    assert (
        await client.get("/api/v1/config/account_a/strategy")
    ).status_code == 404
