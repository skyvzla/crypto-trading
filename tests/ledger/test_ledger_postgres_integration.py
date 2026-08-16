"""需要 LEDGER_TEST_DSN 指向可清理的真实 PostgreSQL。"""

import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI
from psycopg.errors import StringDataRightTruncation

from trading_platform.ledger.api.routes import router
from trading_platform.ledger.binance_account_updates import BinanceAccountUpdateLedger
from trading_platform.ledger.binance_reports import BinanceExecutionReportLedger
from trading_platform.ledger.binance_runtime import BinanceLedgerCallbacks
from trading_platform.ledger.db.models import (
    CampaignPnLFactsError,
    LedgerDB,
    Order,
    Position,
    Trade,
    create_connection_pool,
)
from trading_platform.ledger.db.migrations import apply_migrations, verify_current
from trading_platform.shared.binance import BinanceOrderExecutor
from trading_platform.shared.events import (
    Order as StrategyOrder,
    OrderIntent,
    StrategyAuditEvent,
)
from trading_platform.shared.execution_recovery import OrderWAL, OrderWALRecord
from trading_platform.shared.risk import RiskConfig, RiskGuard
from trading_platform.strategies.admission import SubcategoryAdmissionService

pytestmark = pytest.mark.skipif(
    not os.getenv("LEDGER_TEST_DSN"),
    reason="LEDGER_TEST_DSN not set",
)


@pytest.fixture
async def ledger():
    pool = await create_connection_pool(os.environ["LEDGER_TEST_DSN"], 1, 4)
    await apply_migrations(pool)
    await verify_current(pool)
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
async def test_web_order_activity_and_trade_calendar_filters_are_server_side(
    ledger, client
):
    suffix = uuid4().hex[:10]
    account = f"web-filter-{suffix}"
    for status in ("NEW", "FILLED"):
        await ledger.insert_order(
            Order(
                account_id=account,
                strategy_id="s",
                symbol="BTCUSDT",
                order_id=f"order-{status.lower()}",
                client_order_id=f"client-{status.lower()}-{suffix}",
                side="BUY",
                order_type="LIMIT",
                quantity=Decimal("1"),
                price=Decimal("100"),
                status=status,
            )
        )

    for index, moment in enumerate(
        (
            datetime(2026, 8, 1, 15, 30, tzinfo=timezone.utc),
            datetime(2026, 8, 1, 16, 30, tzinfo=timezone.utc),
        ),
        start=1,
    ):
        await ledger.insert_trade(
            Trade(
                account_id=account,
                strategy_id="s",
                symbol="BTCUSDT",
                trade_id=f"trade-{index}",
                order_id=f"trade-order-{index}",
                client_order_id=f"trade-client-{index}-{suffix}",
                side="BUY",
                quantity=Decimal("1"),
                price=Decimal("100"),
                quote_quantity=Decimal("100"),
                commission=Decimal("0.1"),
                commission_asset="USDT",
                realized_pnl=Decimal("1"),
                exchange_time=moment,
            )
        )

    active = (
        await client.get(
            "/api/v1/orders",
            params={"account_id": account, "active_only": True},
        )
    ).json()
    assert active["total"] == 1
    assert active["items"][0]["status"] == "NEW"

    shanghai_day = (
        await client.get(
            "/api/v1/trades",
            params={
                "account_id": account,
                "start_date": "2026-08-02",
                "end_date": "2026-08-02",
                "timezone": "Asia/Shanghai",
            },
        )
    ).json()
    assert shanghai_day["total"] == 1
    assert shanghai_day["items"][0]["trade_id"] == "trade-2"


@pytest.mark.asyncio
async def test_daily_pnl_timezone_boundaries_and_campaign_performance(
    ledger, client
):
    suffix = uuid4().hex[:10]
    account = f"web-{suffix}"
    strategy = f"perf-{suffix}"

    # The two fills fall on different Shanghai calendar dates but the same UTC date.
    for index, moment in enumerate(
        (
            datetime(2026, 8, 1, 15, 30, tzinfo=timezone.utc),
            datetime(2026, 8, 1, 16, 30, tzinfo=timezone.utc),
        ),
        start=1,
    ):
        assert await ledger.insert_trade(
            Trade(
                account_id=account,
                strategy_id=strategy,
                symbol="TZUSDT",
                trade_id=f"tz-{index}",
                order_id=f"tz-order-{index}",
                client_order_id=f"tz-client-{index}-{suffix}",
                side="SELL",
                quantity=Decimal("1"),
                price=Decimal("100"),
                quote_quantity=Decimal("100"),
                commission=Decimal("0.1"),
                commission_asset="USDT",
                realized_pnl=Decimal(str(index)),
                is_maker=False,
                exchange_time=moment,
            )
        ) > 0

    shanghai = (
        await client.get(
            "/api/v1/pnl/daily",
            params={
                "account_id": account,
                "start_date": "2026-08-02",
                "end_date": "2026-08-02",
            },
        )
    ).json()
    assert len(shanghai) == 1
    assert shanghai[0]["date"] == "2026-08-02"
    assert shanghai[0]["timezone"] == "Asia/Shanghai"
    assert Decimal(shanghai[0]["net_pnl"]) == Decimal("1.9")

    utc = (
        await client.get(
            "/api/v1/pnl/daily",
            params={
                "account_id": account,
                "start_date": "2026-08-01",
                "end_date": "2026-08-01",
                "timezone": "UTC",
            },
        )
    ).json()
    assert len(utc) == 1
    assert utc[0]["date"] == "2026-08-01"
    assert utc[0]["timezone"] == "UTC"
    assert Decimal(utc[0]["net_pnl"]) == Decimal("2.8")

    # Campaign metrics are calculated from complete round trips, not individual fills.
    for campaign_id, side_values in {
        "win": (("SELL", "6"), ("BUY", "0")),
        "loss": (("SELL", "-4"), ("BUY", "0")),
        "open": (("SELL", "3"),),
    }.items():
        for index, (side, realized) in enumerate(side_values, start=1):
            moment = datetime(
                2026, 8, 3, 12 + index, tzinfo=timezone.utc
            )
            assert await ledger.insert_trade(
                Trade(
                    account_id=account,
                    strategy_id=strategy,
                    symbol="PERFUSDT",
                    trade_id=f"{campaign_id}-{index}",
                    order_id=f"{campaign_id}-order-{index}",
                    client_order_id=f"{campaign_id}-client-{index}-{suffix}",
                    campaign_id=campaign_id,
                    side=side,
                    quantity=Decimal("1"),
                    price=Decimal("100"),
                    quote_quantity=Decimal("100"),
                    commission=Decimal("0.1"),
                    commission_asset="USDT",
                    realized_pnl=Decimal(realized),
                    is_maker=False,
                    exchange_time=moment,
                )
            ) > 0

    performance = (
        await client.get(
            "/api/v1/performance",
            params={
                "account_id": account,
                "strategy_id": strategy,
                "start_date": "2026-08-03",
                "end_date": "2026-08-03",
            },
        )
    ).json()
    assert performance["total_trades"] == 2
    assert performance["candidate_campaigns"] == 3
    assert performance["excluded_campaigns"] == 1
    assert performance["win_count"] == 1
    assert performance["loss_count"] == 1
    assert performance["timezone"] == "Asia/Shanghai"
    assert Decimal(performance["net_pnl"]) == Decimal("1.6")


@pytest.mark.asyncio
async def test_performance_breakdown_uses_complete_campaigns_and_synced_dimensions(
    ledger, client
):
    suffix = uuid4().hex[:10].upper()
    account = f"breakdown-{suffix}"
    strategy = f"breakdown-{suffix.lower()}"
    symbol = f"BD{suffix}USDT"
    category_key = f"TEST:CATEGORY:{suffix}"
    subcategory_key = f"TEST:SUBCATEGORY:{suffix}"

    async with ledger.pool.connection() as conn:
        await conn.execute(
            """
            INSERT INTO exchange_symbols (
                symbol, pair, contract_type, status, base_asset, quote_asset,
                margin_asset, underlying_type, raw_metadata, active
            ) VALUES (%s, %s, 'PERPETUAL', 'TRADING', %s, 'USDT', 'USDT',
                'COIN', '{}'::jsonb, TRUE)
            ON CONFLICT (symbol) DO NOTHING
            """,
            (symbol, symbol, symbol.removesuffix("USDT")),
        )
        await conn.execute(
            """
            INSERT INTO exchange_categories (
                category_key, source, category_type, code, name, parent_key
            ) VALUES (%s, 'TEST', 'CATEGORY', %s, %s, NULL),
                     (%s, 'TEST', 'SUBCATEGORY', %s, %s, %s)
            ON CONFLICT (category_key) DO NOTHING
            """,
            (
                category_key,
                suffix,
                f"Category {suffix}",
                subcategory_key,
                suffix,
                f"Subcategory {suffix}",
                category_key,
            ),
        )
        await conn.execute(
            """
            INSERT INTO exchange_symbol_categories (symbol, category_key, active)
            VALUES (%s, %s, TRUE), (%s, %s, TRUE)
            ON CONFLICT (symbol, category_key) DO UPDATE SET active = TRUE
            """,
            (symbol, category_key, symbol, subcategory_key),
        )
        await conn.commit()

    campaigns = {
        "cross-boundary": (
            ("SELL", "6", datetime(2026, 8, 2, 23, tzinfo=timezone.utc)),
            ("BUY", "0", datetime(2026, 8, 3, 1, tzinfo=timezone.utc)),
        ),
        "loss": (
            ("SELL", "-4", datetime(2026, 8, 3, 2, tzinfo=timezone.utc)),
            ("BUY", "0", datetime(2026, 8, 3, 3, tzinfo=timezone.utc)),
        ),
        "open": (("SELL", "3", datetime(2026, 8, 3, 4, tzinfo=timezone.utc)),),
    }
    for campaign_id, fills in campaigns.items():
        for index, (fill_side, realized, moment) in enumerate(fills, start=1):
            assert await ledger.insert_trade(
                Trade(
                    account_id=account,
                    strategy_id=strategy,
                    symbol=symbol,
                    trade_id=f"{campaign_id}-{index}",
                    order_id=f"{campaign_id}-order-{index}",
                    client_order_id=f"{campaign_id}-client-{index}",
                    campaign_id=campaign_id,
                    side=fill_side,
                    quantity=Decimal("1"),
                    price=Decimal("100"),
                    quote_quantity=Decimal("100"),
                    commission=Decimal("0.1"),
                    commission_asset="USDT",
                    realized_pnl=Decimal(realized),
                    is_maker=False,
                    exchange_time=moment,
                )
            ) > 0

    # Same symbol/strategy in another account must never enter this account's
    # performance aggregate, even when the Campaign and time window match.
    for index, (fill_side, realized) in enumerate(
        (("SELL", "100"), ("BUY", "0")), start=1
    ):
        assert await ledger.insert_trade(
            Trade(
                account_id=f"other-{suffix}",
                strategy_id=strategy,
                symbol=symbol,
                trade_id=f"other-account-{index}",
                order_id=f"other-account-order-{index}",
                client_order_id=f"other-account-client-{index}",
                campaign_id="other-account-campaign",
                side=fill_side,
                quantity=Decimal("1"),
                price=Decimal("100"),
                quote_quantity=Decimal("100"),
                commission=Decimal("0.1"),
                commission_asset="USDT",
                realized_pnl=Decimal(realized),
                is_maker=False,
                exchange_time=datetime(2026, 8, 3, 5 + index, tzinfo=timezone.utc),
            )
        ) > 0

    base_params = {
        "account_id": account,
        "strategy_id": strategy,
        "start_date": "2026-08-03",
        "end_date": "2026-08-03",
    }
    symbol_rows = (
        await client.get(
            "/api/v1/performance/breakdown",
            params={**base_params, "group_by": "symbol"},
        )
    ).json()
    category_rows = (
        await client.get(
            "/api/v1/performance/breakdown",
            params={**base_params, "group_by": "category"},
        )
    ).json()
    subcategory_rows = (
        await client.get(
            "/api/v1/performance/breakdown",
            params={**base_params, "group_by": "subcategory"},
        )
    ).json()
    side_rows = (
        await client.get(
            "/api/v1/performance/breakdown",
            params={**base_params, "group_by": "side"},
        )
    ).json()

    for payload, key in (
        (symbol_rows, symbol),
        (category_rows, category_key),
        (subcategory_rows, subcategory_key),
        (side_rows, "SHORT"),
    ):
        assert payload["dimension_available"] is True
        assert payload["timezone"] == "Asia/Shanghai"
        assert len(payload["items"]) == 1
        item = payload["items"][0]
        assert item["dimension_key"] == key
        assert item["total_trades"] == 2
        assert item["candidate_campaigns"] == 3
        assert item["excluded_campaigns"] == 1
        assert Decimal(item["net_pnl"]) == Decimal("1.6")

    filtered = (
        await client.get(
            "/api/v1/performance/breakdown",
            params={
                **base_params,
                "group_by": "symbol",
                "subcategory_key": subcategory_key,
                "side": "SHORT",
            },
        )
    ).json()
    assert filtered["items"][0]["dimension_key"] == symbol


@pytest.mark.asyncio
async def test_strategy_audit_batch_is_atomic_idempotent_and_queryable(ledger, client):
    suffix = uuid4().hex[:10]
    account = f"a-{suffix}"
    events = (
        StrategyAuditEvent(
            event_time=1_700_000_000_001,
            event_type="signal_triggered",
            symbol="AKEUSDT",
            strategy_id="spike_short",
            campaign_id=f"spike_short:AKEUSDT:{suffix}",
            details={"trigger_price": "0.1234", "checks": 3},
        ),
        StrategyAuditEvent(
            event_time=1_700_000_000_002,
            event_type="entry_plan_created",
            symbol="AKEUSDT",
            strategy_id="spike_short",
            campaign_id=f"spike_short:AKEUSDT:{suffix}",
            details={"tiers": ["0.12", "0.11"]},
        ),
    )

    assert await ledger.insert_strategy_audit_events(events, account_id=account) == 2
    assert await ledger.insert_strategy_audit_events(events, account_id=account) == 0
    async with ledger.pool.connection() as conn:
        rows = await (
            await conn.execute(
                "SELECT event_type, details FROM strategy_audit_events "
                "WHERE account_id = %s ORDER BY event_time",
                (account,),
            )
        ).fetchall()
    assert rows == [
        ("signal_triggered", {"checks": 3, "trigger_price": "0.1234"}),
        ("entry_plan_created", {"tiers": ["0.12", "0.11"]}),
    ]
    response = await client.get(
        "/api/v1/strategy-audit-events",
        params={
            "account_id": account,
            "campaign_id": f"spike_short:AKEUSDT:{suffix}",
            "event_type": "signal_triggered",
        },
    )
    assert response.status_code == 200
    page = response.json()
    assert page["total"] == 1
    assert page["items"][0]["details"] == {
        "checks": 3,
        "trigger_price": "0.1234",
    }

    rollback_account = f"rollback-{suffix}"
    invalid = StrategyAuditEvent(
        event_time=1_700_000_000_003,
        event_type="x" * 65,
        symbol="AKEUSDT",
        strategy_id="spike_short",
        campaign_id=None,
        details={},
    )
    with pytest.raises(StringDataRightTruncation, match="value too long"):
        await ledger.insert_strategy_audit_events(
            (events[0], invalid), account_id=rollback_account
        )
    async with ledger.pool.connection() as conn:
        count = await (
            await conn.execute(
                "SELECT COUNT(*) FROM strategy_audit_events WHERE account_id = %s",
                (rollback_account,),
            )
        ).fetchone()
    assert count == (0,)


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


@pytest.mark.asyncio
async def test_binance_execution_report_is_atomically_idempotent(ledger):
    suffix = uuid4().hex[:10]
    account_id = f"account-{suffix}"
    campaign_id = "spike_short:BTCUSDT:1779999999000"
    writer = BinanceExecutionReportLedger(
        ledger,
        account_id=account_id,
        strategy_id="spike_short",
    )
    order_data = {
        "s": "BTCUSDT",
        "c": f"client-{suffix}",
        "i": 123456,
        "S": "SELL",
        "o": "LIMIT",
        "X": "FILLED",
        "x": "TRADE",
        "ps": "SHORT",
        "q": "1.5",
        "p": "100",
        "sp": "0",
        "ap": "99.5",
        "z": "1.5",
        "l": "1.5",
        "L": "99.5",
        "Y": "149.25",
        "n": "0.03",
        "N": "USDT",
        "rp": "0.75",
        "m": True,
        "t": 987654,
        "T": 1780000000000,
        "O": 1779999999000,
    }
    record = OrderWALRecord(
        record_type="exchange_status",
        recorded_at=1780000000000,
        account_id=account_id,
        client_order_id=f"client-{suffix}",
        symbol="BTCUSDT",
        side="SELL",
        order_type="LIMIT",
        quantity="1.5",
        price="100",
        status="FILLED",
        exchange_order_id="123456",
        payload={
            "strategy_id": "spike_short",
            "campaign_id": campaign_id,
        },
    )

    first_order, first_trade = await writer.handle(order_data, record)
    second_order, second_trade = await writer.handle(order_data, record)

    assert first_order == second_order
    assert first_trade and second_trade == 0
    assert await ledger.count_orders(account_id=account_id) == 1
    assert await ledger.count_trades(account_id=account_id) == 1
    stored = (await ledger.get_orders(account_id=account_id))[0]
    assert stored.campaign_id == campaign_id
    assert stored.status == "FILLED"
    assert stored.filled_quantity == Decimal("1.5")
    trade = (await ledger.get_trades(account_id=account_id))[0]
    assert trade.realized_pnl == Decimal("0.75")


@pytest.mark.asyncio
async def test_campaign_trade_query_is_scoped_to_wal_client_order_ids(ledger):
    suffix = uuid4().hex[:10]
    account_id = f"campaign-{suffix}"
    campaign_id = f"spike_short:AKEUSDT:{int(uuid4().int % 10**12)}"
    now = datetime.now(timezone.utc)
    for trade_id, client_order_id, commission in (
        ("1", f"entry-{suffix}", Decimal("0.01")),
        ("2", f"exit-{suffix}", Decimal("0.02")),
        ("3", f"unrelated-{suffix}", Decimal("99")),
    ):
        await ledger.insert_trade(
            Trade(
                account_id=account_id,
                strategy_id="spike_short",
                symbol="AKEUSDT",
                trade_id=trade_id,
                client_order_id=client_order_id,
                campaign_id=campaign_id,
                side="SELL",
                quantity=Decimal("1"),
                price=Decimal("1"),
                quote_quantity=Decimal("1"),
                commission=commission,
                commission_asset="USDT",
                exchange_time=now,
            )
        )

    trades = await ledger.get_trades_by_client_order_ids(
        account_id=account_id,
        strategy_id="spike_short",
        symbol="AKEUSDT",
        campaign_id=campaign_id,
        client_order_ids=[f"entry-{suffix}", f"exit-{suffix}"],
    )

    assert {trade.client_order_id for trade in trades} == {
        f"entry-{suffix}",
        f"exit-{suffix}",
    }
    assert sum((trade.commission for trade in trades), Decimal("0")) == Decimal(
        "0.03"
    )


@pytest.mark.asyncio
async def test_campaign_pnl_handles_partial_reductions_close_and_idempotency(
    ledger, client
):
    suffix = uuid4().hex[:8]
    account_id = f"pnl-{suffix}"
    campaign_id = f"spike_short:AKEUSDT:{int(uuid4().int % 10**12)}"
    started_ms = 1_780_000_000_000
    released_ms = started_ms + 5_000
    base_time = datetime.fromtimestamp(started_ms / 1000, tz=timezone.utc)
    await ledger.insert_strategy_audit_events(
        [
            StrategyAuditEvent(
                event_time=started_ms,
                event_type="campaign_acquired",
                symbol="AKEUSDT",
                strategy_id="spike_short",
                campaign_id=campaign_id,
                details={},
            )
        ],
        account_id=account_id,
    )

    async def insert_fill(
        trade_id: str,
        side: str,
        quantity: str,
        price: str,
        commission: str,
        realized_pnl: str,
        offset_seconds: int,
    ) -> int:
        quantity_value = Decimal(quantity)
        price_value = Decimal(price)
        return await ledger.insert_trade(
            Trade(
                account_id=account_id,
                strategy_id="spike_short",
                symbol="AKEUSDT",
                trade_id=trade_id,
                order_id=f"order-{trade_id}",
                client_order_id=f"client-{suffix}-{trade_id}",
                campaign_id=campaign_id,
                side=side,
                position_side="BOTH",
                quantity=quantity_value,
                price=price_value,
                quote_quantity=quantity_value * price_value,
                commission=Decimal(commission),
                commission_asset="USDT",
                realized_pnl=Decimal(realized_pnl),
                is_maker=False,
                exchange_time=base_time + timedelta(seconds=offset_seconds),
            )
        )

    await insert_fill("sell-1", "SELL", "1", "100", "0.01", "0", 1)
    await insert_fill("sell-2", "SELL", "0.5", "102", "0.01", "0", 2)
    await insert_fill("buy-1", "BUY", "0.5", "90", "0.02", "5", 3)
    await insert_fill("buy-2", "BUY", "0.5", "80", "0.02", "10", 4)

    partial = await ledger.get_campaign_pnl(
        account_id=account_id,
        strategy_id="spike_short",
        campaign_id=campaign_id,
    )
    assert partial is not None
    assert partial.trade_count == 4
    assert partial.sell_quantity == Decimal("1.5")
    assert partial.sell_avg_price.quantize(Decimal("0.00000001")) == Decimal(
        "100.66666667"
    )
    assert partial.buy_quantity == Decimal("1.0")
    assert partial.buy_avg_price == Decimal("85")
    assert partial.total_commission == Decimal("0.06")
    assert partial.gross_realized_pnl == Decimal("15")
    assert partial.net_realized_pnl == Decimal("14.94")
    assert partial.remaining_quantity == Decimal("0.5")
    assert partial.has_open_quantity is True
    assert partial.closed_at is None
    assert partial.released_at is None

    assert await insert_fill(
        "buy-2", "BUY", "0.5", "80", "0.02", "10", 4
    ) == 0
    await insert_fill("buy-3", "BUY", "0.5", "70", "0.02", "15", 5)
    await ledger.insert_strategy_audit_events(
        [
            StrategyAuditEvent(
                event_time=released_ms,
                event_type="campaign_released",
                symbol="AKEUSDT",
                strategy_id="spike_short",
                campaign_id=campaign_id,
                details={},
            )
        ],
        account_id=account_id,
    )

    not_owned = await client.get(
        f"/api/v1/campaigns/{campaign_id}/pnl",
        params={"account_id": "other-account", "strategy_id": "spike_short"},
    )
    assert not_owned.status_code == 404
    response = await client.get(
        f"/api/v1/campaigns/{campaign_id}/pnl",
        params={"account_id": account_id, "strategy_id": "spike_short"},
    )
    assert response.status_code == 200
    complete = response.json()
    assert complete["trade_count"] == 5
    assert Decimal(complete["sell_quantity"]) == Decimal("1.5")
    assert Decimal(complete["buy_quantity"]) == Decimal("1.5")
    assert Decimal(complete["buy_avg_price"]) == Decimal("80")
    assert Decimal(complete["total_commission"]) == Decimal("0.08")
    assert Decimal(complete["gross_realized_pnl"]) == Decimal("30")
    assert Decimal(complete["net_realized_pnl"]) == Decimal("29.92")
    assert Decimal(complete["remaining_quantity"]) == Decimal("0")
    assert complete["has_open_quantity"] is False
    assert complete["closed_at"] is not None
    assert complete["acquired_at"] is not None
    assert complete["released_at"] is not None
    assert complete["lifecycle_duration_ms"] == 5_000


@pytest.mark.asyncio
async def test_order_and_trade_campaign_attribution_is_immutable(ledger):
    suffix = uuid4().hex[:8]
    account_id = f"immutable-{suffix}"
    now = datetime.now(timezone.utc)
    order = Order(
        account_id=account_id,
        strategy_id="spike_short",
        symbol="AKEUSDT",
        order_id=f"order-{suffix}",
        client_order_id=f"client-{suffix}",
        campaign_id=f"spike_short:AKEUSDT:{int(uuid4().int % 10**12)}",
        side="SELL",
        order_type="LIMIT",
        quantity=Decimal("1"),
        price=Decimal("1"),
        status="FILLED",
        exchange_created_at=now,
    )
    trade = Trade(
        account_id=account_id,
        strategy_id="spike_short",
        symbol="AKEUSDT",
        trade_id=f"trade-{suffix}",
        order_id=order.order_id,
        client_order_id=order.client_order_id,
        campaign_id=order.campaign_id,
        side="SELL",
        position_side="BOTH",
        quantity=Decimal("1"),
        price=Decimal("1"),
        quote_quantity=Decimal("1"),
        commission=Decimal("0.01"),
        commission_asset="USDT",
        realized_pnl=Decimal("0"),
        exchange_time=now,
    )
    await ledger.insert_order(order)
    await ledger.insert_trade(trade)
    other_campaign = f"spike_short:AKEUSDT:{int(uuid4().int % 10**12)}"

    with pytest.raises(ValueError, match="order Campaign attribution is immutable"):
        await ledger.insert_order(replace(order, campaign_id=other_campaign))
    with pytest.raises(ValueError, match="trade facts are immutable"):
        await ledger.insert_trade(replace(trade, campaign_id=other_campaign))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("side", "commission_asset"),
    [("SELL", "BNB"), ("BUY", "USDT")],
)
async def test_campaign_pnl_rejects_unconvertible_or_inconsistent_facts(
    ledger, client, side, commission_asset
):
    suffix = uuid4().hex[:8]
    campaign_id = f"spike_short:AKEUSDT:{int(uuid4().int % 10**12)}"
    await ledger.insert_trade(
        Trade(
            account_id=f"invalid-pnl-{suffix}",
            strategy_id="spike_short",
            symbol="AKEUSDT",
            trade_id=f"trade-{suffix}",
            order_id=f"order-{suffix}",
            client_order_id=f"client-{suffix}",
            campaign_id=campaign_id,
            side=side,
            position_side="BOTH",
            quantity=Decimal("1"),
            price=Decimal("1"),
            quote_quantity=Decimal("1"),
            commission=Decimal("0.01"),
            commission_asset=commission_asset,
            realized_pnl=Decimal("0"),
            exchange_time=datetime.now(timezone.utc),
        )
    )

    with pytest.raises(CampaignPnLFactsError, match="requires realized PnL"):
        await ledger.get_campaign_pnl(
            account_id=f"invalid-pnl-{suffix}",
            strategy_id="spike_short",
            campaign_id=campaign_id,
        )

    response = await client.get(
        f"/api/v1/campaigns/{campaign_id}/pnl",
        params={
            "account_id": f"invalid-pnl-{suffix}",
            "strategy_id": "spike_short",
        },
    )
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_campaign_pnl_rejects_multiple_symbols(ledger):
    suffix = uuid4().hex[:8]
    account_id = f"multi-symbol-{suffix}"
    campaign_id = f"spike_short:AKEUSDT:{int(uuid4().int % 10**12)}"
    for index, symbol in enumerate(("AKEUSDT", "BTCUSDT"), start=1):
        await ledger.insert_trade(
            Trade(
                account_id=account_id,
                strategy_id="spike_short",
                symbol=symbol,
                trade_id=str(index),
                order_id=f"order-{index}",
                client_order_id=f"client-{index}",
                campaign_id=campaign_id,
                side="SELL",
                quantity=Decimal("1"),
                price=Decimal("1"),
                quote_quantity=Decimal("1"),
                commission=Decimal("0.01"),
                commission_asset="USDT",
                realized_pnl=Decimal("0"),
                exchange_time=datetime.now(timezone.utc),
            )
        )

    with pytest.raises(CampaignPnLFactsError, match="multiple symbols"):
        await ledger.get_campaign_pnl(
            account_id=account_id,
            strategy_id="spike_short",
            campaign_id=campaign_id,
        )


@pytest.mark.asyncio
async def test_duplicate_trade_id_rejects_changed_accounting_facts(ledger):
    suffix = uuid4().hex[:8]
    trade = Trade(
        account_id=f"immutable-{suffix}",
        strategy_id="spike_short",
        symbol="AKEUSDT",
        trade_id=f"trade-{suffix}",
        order_id=f"order-{suffix}",
        client_order_id=f"client-{suffix}",
        campaign_id=f"spike_short:AKEUSDT:{int(uuid4().int % 10**12)}",
        side="SELL",
        quantity=Decimal("1"),
        price=Decimal("1"),
        quote_quantity=Decimal("1"),
        commission=Decimal("0.01"),
        commission_asset="USDT",
        realized_pnl=Decimal("0"),
        exchange_time=datetime.now(timezone.utc),
    )
    assert await ledger.insert_trade(trade) > 0
    assert await ledger.insert_trade(trade) == 0

    with pytest.raises(ValueError, match="trade facts are immutable"):
        await ledger.insert_trade(replace(trade, commission=Decimal("0.02")))


@pytest.mark.asyncio
async def test_binance_account_update_persists_signed_snapshot_and_rejects_stale(
    ledger, client
):
    suffix = uuid4().hex[:10]
    account_id = f"account-{suffix}"
    writer = BinanceAccountUpdateLedger(
        ledger,
        account_id=account_id,
        strategy_id="spike_short",
    )
    await ledger.upsert_position(
        Position(
            account_id=account_id,
            strategy_id="spike_short",
            symbol="BTCUSDT",
            position_side="SHORT",
            quantity=Decimal("-0.25"),
            entry_price=Decimal("101"),
            mark_price=Decimal("99"),
            liquidation_price=Decimal("150"),
            leverage=5,
        )
    )

    def event(transaction_time, quantity, unrealized_pnl):
        return {
            "e": "ACCOUNT_UPDATE",
            "E": transaction_time + 10,
            "T": transaction_time,
            "a": {
                "m": "ORDER",
                "B": [{"a": "USDT", "wb": "100", "cw": "90", "bc": "0"}],
                "P": [{
                    "s": "BTCUSDT",
                    "pa": quantity,
                    "ep": "100.25",
                    "bep": "100.20",
                    "cr": "0.75",
                    "up": unrealized_pnl,
                    "mt": "isolated",
                    "iw": "10",
                    "ps": "SHORT",
                    "ma": "USDT",
                }],
            },
        }

    first = await writer.handle(event(1780000000000, "-1.5", "2.5"))
    duplicate = await writer.handle(event(1780000000000, "-1.5", "2.5"))
    stale = await writer.handle(event(1779999999000, "-0.5", "0.1"))

    assert first[0] > 0
    assert duplicate == first
    assert stale == [0]
    assert await ledger.count_positions(account_id=account_id) == 1
    stored = (await ledger.get_positions(account_id=account_id))[0]
    assert stored.quantity == Decimal("-1.5")
    assert stored.unrealized_pnl == Decimal("2.5")
    assert stored.mark_price == Decimal("99")
    assert stored.liquidation_price == Decimal("150")
    assert stored.leverage == 5
    assert stored.exchange_time == datetime.fromtimestamp(
        1780000000000 / 1000, tz=timezone.utc
    )
    response = await client.get(
        "/api/v1/positions",
        params={"account_id": account_id, "strategy_id": "spike_short"},
    )
    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert Decimal(response.json()["items"][0]["quantity"]) == Decimal("-1.5")

    await writer.handle(event(1780000001000, "0", "0"))
    assert await ledger.count_positions(account_id=account_id) == 0
    assert await ledger.get_positions(account_id=account_id) == []


@pytest.mark.asyncio
async def test_binance_runtime_callbacks_close_wal_order_trade_position_loop(
    ledger, tmp_path
):
    suffix = uuid4().hex[:10]
    account_id = f"account-{suffix}"
    client_order_id = f"client-{suffix}"
    campaign_id = "spike_short:BTCUSDT:1779999998000"
    wal = OrderWAL(tmp_path / "orders.jsonl")
    intent = wal.record_intent(
        OrderIntent(
            symbol="BTCUSDT",
            side="SELL",
            price=Decimal("100"),
            quantity=Decimal("1.5"),
            client_order_id=client_order_id,
            strategy_id="spike_short",
            campaign_id=campaign_id,
        ),
        account_id=account_id,
        recorded_at=1779999998000,
    )
    wal.record_submit_unknown(
        intent,
        recorded_at=1779999999000,
        error="timeout",
    )
    guard = RiskGuard(account_id, RiskConfig())
    guard.block_symbol("BTCUSDT", f"SUBMIT_UNKNOWN:{client_order_id}")
    executor = BinanceOrderExecutor(
        Mock(post_order=AsyncMock(), query_order=AsyncMock()),
        wal,
        account_id=account_id,
        now_ms=lambda: 1780000000000,
        risk_guard=guard,
    )
    callbacks = BinanceLedgerCallbacks(
        executor,
        BinanceExecutionReportLedger(
            ledger, account_id=account_id, strategy_id="spike_short"
        ),
        BinanceAccountUpdateLedger(
            ledger, account_id=account_id, strategy_id="spike_short"
        ),
    )
    order_data = {
        "s": "BTCUSDT", "c": client_order_id, "i": 123456,
        "S": "SELL", "o": "LIMIT", "X": "FILLED", "x": "TRADE",
        "ps": "SHORT", "q": "1.5", "p": "100", "sp": "0",
        "ap": "99.5", "z": "1.5", "l": "1.5", "L": "99.5",
        "Y": "149.25", "n": "0.03", "N": "USDT", "rp": "0.75",
        "m": True, "t": 987654, "T": 1780000000000,
        "O": 1779999999000,
    }
    account_event = {
        "e": "ACCOUNT_UPDATE", "E": 1780000000010, "T": 1780000000000,
        "a": {
            "m": "ORDER",
            "B": [],
            "P": [{
                "s": "BTCUSDT", "pa": "-1.5", "ep": "99.5",
                "bep": "99.5", "cr": "0.75", "up": "2.5",
                "mt": "isolated", "iw": "10", "ps": "SHORT",
                "ma": "USDT",
            }],
        },
    }

    await callbacks.handle_execution_report(order_data)
    await callbacks.handle_account_update(account_event)

    assert wal.recover_latest()[client_order_id].status == "FILLED"
    assert "BTCUSDT" not in guard.blocked_symbols
    assert await ledger.count_orders(account_id=account_id) == 1
    assert await ledger.count_trades(account_id=account_id) == 1
    assert (
        await ledger.get_orders(account_id=account_id)
    )[0].campaign_id == campaign_id
    assert (
        await ledger.get_trades(account_id=account_id)
    )[0].campaign_id == campaign_id
    positions = await ledger.get_positions(account_id=account_id)
    assert len(positions) == 1
    assert positions[0].quantity == Decimal("-1.5")


@pytest.mark.asyncio
async def test_subcategory_admission_service_reads_db_and_cancels_on_close(ledger):
    subcategory = f"spike-{uuid4().hex[:10]}"
    admission = await ledger.set_subcategory_admission(
        subcategory,
        True,
        expected_version=0,
        updated_by="integration-test",
    )
    gate = Mock(set_entry_enabled=Mock())
    account = Mock(
        iter_orders=Mock(return_value=(
            StrategyOrder(
                order_id="entry-1",
                client_order_id="client-entry-1",
                account_id="account-1",
                symbol="BTCUSDT",
                side="SELL",
                type="LIMIT",
                price=Decimal("100"),
                quantity=Decimal("1"),
                status="NEW",
                created_at=1_000,
                strategy_id="spike_short",
                trigger_reason="spike_tier1",
            ),
        )),
        cancel_order=Mock(return_value=True),
    )
    service = SubcategoryAdmissionService(
        source=ledger,
        gate=gate,
        account=account,
        subcategory=subcategory,
        strategy_id="spike_short",
        entry_trigger_reasons={"spike_tier1", "spike_tier2", "spike_tier3"},
    )

    opened = await service.refresh_once()
    await ledger.set_subcategory_admission(
        subcategory,
        False,
        expected_version=admission.version,
        updated_by="integration-test",
    )
    closed = await service.refresh_once()

    assert opened.enabled is True
    assert closed.enabled is False
    assert gate.set_entry_enabled.call_args_list[-2:] == [((True,),), ((False,),)]
    account.cancel_order.assert_called_once_with("entry-1")


@pytest.mark.asyncio
async def test_exchange_symbol_sync_updates_lifecycle_categories_and_admission(
    ledger, client
):
    # 上一轮运行会写入 20 个 bulk symbol，导致 50% 缩减保护误触发；先清理同步状态。
    async with ledger.pool.connection() as conn:
        await conn.execute("TRUNCATE exchange_symbol_sync_state")
        await conn.commit()
    suffix = uuid4().hex[:8].upper()
    symbol = f"T{suffix}USDT"
    strategy_id = f"spike-{suffix.lower()}"
    subtype = f"TEST_{suffix}"
    first = {
        "symbols": [
            {
                "symbol": symbol,
                "pair": symbol,
                "contractType": "PERPETUAL",
                "status": "TRADING",
                "onboardDate": 1_564_611_200_000,
                "deliveryDate": 4_133_404_800_000,
                "baseAsset": "BTC",
                "quoteAsset": "USDT",
                "marginAsset": "USDT",
                "underlyingType": "COIN",
                "underlyingSubType": [subtype, "LAYER_1"],
            },
            {
                "symbol": "HFTUSDT",
                "pair": "HFTUSDT",
                "contractType": "PERPETUAL",
                "status": "SETTLING",
                "onboardDate": 1_680_000_000_000,
                "deliveryDate": 1_786_093_200_000,
                "baseAsset": "HFT",
                "quoteAsset": "USDT",
                "marginAsset": "USDT",
            },
            {
                "symbol": "BTCUSDC",
                "pair": "BTCUSDC",
                "contractType": "PERPETUAL",
                "status": "TRADING",
                "onboardDate": 1_680_000_000_000,
                "deliveryDate": 4_133_404_800_000,
                "baseAsset": "BTC",
                "quoteAsset": "USDC",
                "marginAsset": "USDC",
            },
        ]
    }

    assert await ledger.sync_exchange_symbols(first) == 2
    assert await ledger.get_exchange_symbol("BTCUSDC") is None
    btc = await ledger.get_exchange_symbol(symbol.lower())
    assert btc is not None
    assert btc.base_asset == "BTC"
    assert btc.quote_asset == "USDT"
    assert btc.raw_metadata["underlyingSubType"] == [subtype, "LAYER_1"]
    categories = await ledger.list_exchange_symbol_categories(symbol)
    assert {(item.category_type, item.code) for item in categories} == {
        ("CATEGORY", "COIN"),
        ("SUBCATEGORY", subtype),
        ("SUBCATEGORY", "LAYER_1"),
    }
    category_by_code = {item.code: item for item in categories}
    assert category_by_code[subtype].parent_key == category_by_code["COIN"].category_key

    assert await ledger.list_tradeable_exchange_symbols(strategy_id=strategy_id) == [
        symbol
    ]
    seeded_globals, seeded_categories = await ledger.seed_exchange_symbol_admissions(
        default_disabled_symbols=[symbol, "MISSINGUSDT"],
        legacy_strategy_id=strategy_id,
        updated_by="integration-test",
        default_reason="default block",
        legacy_reason="legacy block",
    )
    assert (seeded_globals, seeded_categories) == (1, 0)
    assert await ledger.list_tradeable_exchange_symbols() == []
    await ledger.set_symbol_global_admission(
        symbol, True, 1, "integration-test", "operator override"
    )
    seeded_globals, seeded_categories = await ledger.seed_exchange_symbol_admissions(
        default_disabled_symbols=[symbol],
        legacy_strategy_id=strategy_id,
        updated_by="integration-test",
        default_reason="default block",
        legacy_reason="legacy block",
    )
    assert (seeded_globals, seeded_categories) == (0, 0)
    assert await ledger.list_tradeable_exchange_symbols() == [symbol]

    legacy = await ledger.set_subcategory_admission(
        subtype, False, 0, "integration-test", "legacy block"
    )
    assert legacy.version == 1
    seeded_globals, seeded_categories = await ledger.seed_exchange_symbol_admissions(
        default_disabled_symbols=[],
        legacy_strategy_id=strategy_id,
        updated_by="integration-test",
        default_reason="default block",
        legacy_reason="legacy block",
    )
    assert (seeded_globals, seeded_categories) == (0, 1)
    assert await ledger.list_tradeable_exchange_symbols(strategy_id=strategy_id) == []
    migrated = await ledger.get_strategy_category_admission(
        strategy_id, category_by_code[subtype].category_key
    )
    assert migrated is not None
    assert migrated.enabled is False
    await ledger.set_strategy_category_admission(
        strategy_id,
        category_by_code[subtype].category_key,
        True,
        migrated.version,
        "integration-test",
    )
    global_control = await ledger.set_symbol_global_admission(
        symbol, False, 2, "test", "manual block"
    )
    assert global_control.version == 3
    assert await ledger.list_tradeable_exchange_symbols() == []
    await ledger.set_symbol_global_admission(symbol, True, 3, "test")

    strategy_control = await ledger.set_strategy_category_admission(
        strategy_id,
        category_by_code[subtype].category_key,
        False,
        2,
        "test",
    )
    assert strategy_control.version == 3
    assert await ledger.list_tradeable_exchange_symbols() == [symbol]
    assert await ledger.list_tradeable_exchange_symbols(
        strategy_id="unconfigured-strategy"
    ) == [symbol]
    assert await ledger.list_tradeable_exchange_symbols(
        strategy_id=strategy_id
    ) == []
    await ledger.set_strategy_category_admission(
        strategy_id,
        category_by_code[subtype].category_key,
        True,
        3,
        "test",
    )
    assert await ledger.list_tradeable_exchange_symbols(
        strategy_id=strategy_id
    ) == [symbol]

    parent_control = await ledger.set_strategy_category_admission(
        strategy_id,
        category_by_code["COIN"].category_key,
        False,
        0,
        "test",
    )
    assert parent_control.version == 1
    assert await ledger.list_tradeable_exchange_symbols(
        strategy_id=strategy_id
    ) == []
    await ledger.set_strategy_category_admission(
        strategy_id,
        category_by_code["COIN"].category_key,
        True,
        1,
        "test",
    )

    symbols_response = await client.get(
        "/api/v1/exchange-symbols", params={"limit": 1000}
    )
    assert symbols_response.status_code == 200
    assert any(
        item["symbol"] == symbol for item in symbols_response.json()["items"]
    )
    unclassified_symbols_response = await client.get(
        "/api/v1/exchange-symbols",
        params={"unclassified": True, "limit": 1000},
    )
    assert unclassified_symbols_response.status_code == 200
    unclassified_symbols = {
        item["symbol"]
        for item in unclassified_symbols_response.json()["items"]
    }
    # HFTUSDT has no Binance underlyingType and therefore no active assignment;
    # the synchronized symbol with Category/Subcategory must not leak into it.
    assert "HFTUSDT" in unclassified_symbols
    assert symbol not in unclassified_symbols
    async with ledger.pool.connection() as conn:
        await conn.execute(
            "INSERT INTO exchange_symbol_categories "
            "(symbol, category_key, active) VALUES (%s, %s, FALSE) "
            "ON CONFLICT (symbol, category_key) DO UPDATE SET active = FALSE",
            ("HFTUSDT", category_by_code["COIN"].category_key),
        )
        await conn.commit()
    inactive_assignment_response = await client.get(
        "/api/v1/exchange-symbols",
        params={"unclassified": True, "limit": 1000},
    )
    assert "HFTUSDT" in {
        item["symbol"] for item in inactive_assignment_response.json()["items"]
    }
    categories_response = await client.get("/api/v1/exchange-categories")
    assert categories_response.status_code == 200
    assert any(item["code"] == subtype for item in categories_response.json())
    category_response = next(
        item
        for item in categories_response.json()
        if item["code"] == subtype
    )
    assert category_response["symbol_count"] == 1
    category_symbols_response = await client.get(
        f"/api/v1/exchange-categories/{category_response['category_key']}/symbols",
        params={"limit": 10},
    )
    assert category_symbols_response.status_code == 200
    assert category_symbols_response.json()["total"] == 1
    assert category_symbols_response.json()["items"][0]["symbol"] == symbol
    sync_status_response = await client.get(
        "/api/v1/exchange-symbol-sync/status"
    )
    assert sync_status_response.status_code == 200
    assert sync_status_response.json()["status"] == "SUCCESS"
    assert sync_status_response.json()["effective_universe_ready"] is True
    strategy_response = await client.get(
        f"/api/v1/strategy-category-admissions/{strategy_id}"
    )
    assert strategy_response.status_code == 200
    assert strategy_response.json()[0]["enabled"] is True
    global_audit = await client.get(
        "/api/v1/symbol-global-admission-audit", params={"symbol": symbol}
    )
    assert global_audit.status_code == 200
    assert global_audit.json()["total"] == 4
    category_audit = await client.get(
        "/api/v1/strategy-category-admission-audit",
        params={"strategy_id": strategy_id},
    )
    assert category_audit.status_code == 200
    assert category_audit.json()["total"] == 6
    preview_response = await client.get(
        f"/api/v1/strategy-category-admissions/{strategy_id}/universe-preview",
        params={"effective": True, "limit": 10},
    )
    assert preview_response.status_code == 200
    preview = preview_response.json()
    # Previous sync snapshots remain as inactive facts and are intentionally
    # included in the preview with an exclusion reason.
    assert preview["total_symbols"] >= 2
    assert preview["effective_symbols"] == 1
    assert preview["excluded_symbols"] == preview["total_symbols"] - 1
    assert preview["items"][0]["symbol"] == symbol
    assert preview["items"][0]["effective"] is True

    hft = await ledger.get_exchange_symbol("hftusdt")
    assert hft is not None
    assert hft.status == "SETTLING"
    assert hft.delivery_date.isoformat() == "2026-08-07T09:00:00+00:00"
    assert hft.active is True

    with pytest.raises(ValueError, match="no valid symbols"):
        await ledger.sync_exchange_symbols({"symbols": []})
    assert (await ledger.get_exchange_symbol("HFTUSDT")).active is True

    with pytest.raises(ValueError, match="incomplete lifecycle metadata"):
        await ledger.sync_exchange_symbols(
            {"symbols": [{"symbol": "BROKENUSDT", "quoteAsset": "USDT"}]}
        )
    assert (await ledger.get_exchange_symbol("HFTUSDT")).active is True

    await ledger.mark_exchange_symbol_sync_failed(RuntimeError("network down"))
    assert await ledger.list_tradeable_exchange_symbols() == []
    assert await ledger.sync_exchange_symbols(first) == 2

    async with ledger.pool.connection() as conn:
        await conn.execute(
            "UPDATE exchange_symbol_sync_state "
            "SET last_success_at = NOW() - INTERVAL '37 hours'"
        )
    assert await ledger.list_tradeable_exchange_symbols() == []
    assert await ledger.sync_exchange_symbols(first) == 2

    assert await ledger.sync_exchange_symbols({"symbols": [first["symbols"][0]]}) == 1
    hft = await ledger.get_exchange_symbol("HFTUSDT")
    assert hft is not None
    assert hft.active is False

    bulk = {
        "symbols": [
            {
                "symbol": f"BULK{index:02d}{suffix}USDT",
                "contractType": "PERPETUAL",
                "status": "TRADING",
                "onboardDate": 1_564_611_200_000,
                "deliveryDate": 4_133_404_800_000,
                "baseAsset": f"BULK{index:02d}{suffix}",
                "quoteAsset": "USDT",
                "marginAsset": "USDT",
            }
            for index in range(20)
        ]
    }
    assert await ledger.sync_exchange_symbols(bulk) == 20
    with pytest.raises(ValueError, match="symbol count dropped"):
        await ledger.sync_exchange_symbols(first)
    assert (await ledger.get_exchange_symbol(bulk["symbols"][0]["symbol"])).active
