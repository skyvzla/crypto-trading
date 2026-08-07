"""需要 LEDGER_TEST_DSN 指向可清理的真实 PostgreSQL。"""

import os
from datetime import datetime, timezone
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
from trading_platform.shared.execution_recovery import OrderWAL
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
    writer = BinanceExecutionReportLedger(
        ledger,
        account_id=f"account-{suffix}",
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

    first_order, first_trade = await writer.handle(order_data)
    second_order, second_trade = await writer.handle(order_data)

    assert first_order == second_order
    assert first_trade and second_trade == 0
    account_id = f"account-{suffix}"
    assert await ledger.count_orders(account_id=account_id) == 1
    assert await ledger.count_trades(account_id=account_id) == 1
    stored = (await ledger.get_orders(account_id=account_id))[0]
    assert stored.status == "FILLED"
    assert stored.filled_quantity == Decimal("1.5")
    trade = (await ledger.get_trades(account_id=account_id))[0]
    assert trade.realized_pnl == Decimal("0.75")


@pytest.mark.asyncio
async def test_campaign_trade_query_is_scoped_to_wal_client_order_ids(ledger):
    suffix = uuid4().hex[:10]
    account_id = f"campaign-{suffix}"
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
    wal = OrderWAL(tmp_path / "orders.jsonl")
    intent = wal.record_intent(
        OrderIntent(
            symbol="BTCUSDT",
            side="SELL",
            price=Decimal("100"),
            quantity=Decimal("1.5"),
            client_order_id=client_order_id,
            strategy_id="spike_short",
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
