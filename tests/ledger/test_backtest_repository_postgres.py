"""回测订单详情和挂单成交率的 PostgreSQL 集成回归测试。"""

from __future__ import annotations

import os
import urllib.parse
from uuid import uuid4

import pytest
from psycopg import sql
from psycopg.types.json import Jsonb

from trading_platform.ledger.db.backtest_repository import BacktestRepository
from trading_platform.ledger.db.migrations import apply_migrations
from trading_platform.ledger.db.models import create_connection_pool


pytestmark = pytest.mark.skipif(
    not os.getenv("LEDGER_TEST_DSN"),
    reason="LEDGER_TEST_DSN not set",
)


@pytest.fixture
async def backtest_repository():
    base_dsn = os.environ["LEDGER_TEST_DSN"]
    admin_pool = await create_connection_pool(base_dsn, 1, 2)
    schema = f"backtest_repository_{uuid4().hex}"
    async with admin_pool.connection() as connection:
        await connection.execute(
            sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema))
        )
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
        yield BacktestRepository(pool), pool
    finally:
        await pool.close()
        cleanup_pool = await create_connection_pool(base_dsn, 1, 2)
        async with cleanup_pool.connection() as connection:
            await connection.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
            )
        await cleanup_pool.close()


async def _seed_open_trade(pool):
    research_id = uuid4()
    trade_row_id = uuid4()
    run_id = f"run-{uuid4().hex}"
    campaign_id = f"campaign-{uuid4().hex}"
    async with pool.connection() as connection:
        await connection.execute(
            """
            INSERT INTO backtest_researches (
                id, source_key, name, strategy_id, status
            ) VALUES (%s, %s, 'integration', 'spike_short', 'completed')
            """,
            (research_id, uuid4().hex),
        )
        await connection.execute(
            """
            INSERT INTO backtest_runs (research_id, run_id, symbol, status)
            VALUES (%s, %s, 'BTCUSDT', 'completed')
            """,
            (research_id, run_id),
        )
        await connection.execute(
            """
            INSERT INTO backtest_trades (
                id, research_id, run_id, trade_id, campaign_id, symbol, side,
                signal_time, entry_time, entry_price, status, strategy_data
            ) VALUES (
                %s, %s, %s, %s, %s, 'BTCUSDT', 'SHORT',
                100, 200, 100, 'OPEN', '{}'::JSONB
            )
            """,
            (trade_row_id, research_id, run_id, f"trade-{uuid4().hex}", campaign_id),
        )
        await connection.execute(
            """
            INSERT INTO backtest_orders (
                research_id, run_id, order_id, campaign_id, symbol, side,
                price, quantity, status, created_at, fill_time, payload
            ) VALUES (%s, %s, 'entry-1', %s, 'BTCUSDT', 'SELL', 100, 2,
                      'PARTIALLY_FILLED', 200, 250, %s)
            """,
            (
                research_id,
                run_id,
                campaign_id,
                Jsonb(
                    {
                        "type": "LIMIT",
                        "client_order_id": "entry-client-1",
                        "account_id": "backtest",
                        "strategy_id": "spike_short",
                        "ttl_ms": 60_000,
                        "reduce_only": False,
                    }
                ),
            ),
        )
        await connection.execute(
            """
            INSERT INTO backtest_orders (
                research_id, run_id, order_id, campaign_id, symbol, side,
                price, quantity, status, created_at, fill_time, payload
            ) VALUES (%s, %s, 'exit-1', NULL, 'BTCUSDT', 'BUY', NULL, 1,
                      'FILLED', 300, 300, %s)
            """,
            (
                research_id,
                run_id,
                Jsonb(
                    {
                        "type": "MARKET",
                        "client_order_id": "exit-client-1",
                        "reduce_only": True,
                    }
                ),
            ),
        )
        await connection.execute(
            """
            INSERT INTO backtest_orders (
                research_id, run_id, order_id, campaign_id, symbol, side,
                price, quantity, status, created_at, payload
            ) VALUES (%s, %s, 'ordinary-limit-exit', %s, 'BTCUSDT', 'BUY',
                      90, 1, 'NEW', 320, %s)
            """,
            (
                research_id,
                run_id,
                campaign_id,
                Jsonb({"type": "LIMIT", "reduce_only": False}),
            ),
        )
        await connection.execute(
            """
            INSERT INTO backtest_fills (
                research_id, run_id, fill_id, order_id, symbol, side,
                price, quantity, commission, fill_time, payload
            ) VALUES (%s, %s, 'fill-entry-1', 'entry-1', 'BTCUSDT', 'SELL',
                      99, 1, 0.1, 250, %s)
            """,
            (research_id, run_id, Jsonb({"commission_asset": "USDT"})),
        )
        await connection.execute(
            """
            INSERT INTO backtest_fills (
                research_id, run_id, fill_id, order_id, symbol, side,
                price, quantity, commission, fill_time, payload
            ) VALUES (%s, %s, 'fill-exit-1', 'exit-1', 'BTCUSDT', 'BUY',
                      90, 1, 0.1, 300, %s)
            """,
            (research_id, run_id, Jsonb({"commission_asset": "USDT"})),
        )
    return research_id, trade_row_id


async def _seed_legacy_trade_windows(pool):
    research_id = uuid4()
    first_trade_id = uuid4()
    second_trade_id = uuid4()
    run_id = f"legacy-{uuid4().hex}"
    async with pool.connection() as connection:
        await connection.execute(
            """
            INSERT INTO backtest_researches (
                id, source_key, name, strategy_id, status
            ) VALUES (%s, %s, 'legacy integration', 'legacy', 'completed')
            """,
            (research_id, uuid4().hex),
        )
        await connection.execute(
            """
            INSERT INTO backtest_runs (research_id, run_id, symbol, status)
            VALUES (%s, %s, 'BTCUSDT', 'completed')
            """,
            (research_id, run_id),
        )
        await connection.execute(
            """
            INSERT INTO backtest_trades (
                id, research_id, run_id, trade_id, campaign_id, symbol, side,
                signal_time, entry_time, exit_time, entry_price, exit_price,
                status, strategy_data
            ) VALUES
                (%s, %s, %s, 'legacy-1', NULL, 'BTCUSDT', 'SHORT',
                 NULL, 100, 200, 100, 90, 'CLOSED', '{}'::JSONB),
                (%s, %s, %s, 'legacy-2', NULL, 'BTCUSDT', 'SHORT',
                 NULL, 200, 300, 90, 80, 'CLOSED', '{}'::JSONB)
            """,
            (
                first_trade_id,
                research_id,
                run_id,
                second_trade_id,
                research_id,
                run_id,
            ),
        )
        await connection.execute(
            """
            INSERT INTO backtest_orders (
                research_id, run_id, order_id, campaign_id, symbol, side,
                price, quantity, status, created_at, fill_time, payload
            ) VALUES
                (%s, %s, 'legacy-entry-filled', NULL, 'BTCUSDT', 'SELL',
                 100, 1, 'FILLED', 90, 100,
                 '{"type":"LIMIT","reduce_only":false}'::JSONB),
                (%s, %s, 'legacy-entry-unfilled', NULL, 'BTCUSDT', 'SELL',
                 101, 1, 'CANCELED', 90, NULL,
                 '{"type":"LIMIT","reduce_only":false,"cancel_time":150}'::JSONB),
                (%s, %s, 'legacy-exit-1', NULL, 'BTCUSDT', 'BUY',
                 NULL, 1, 'FILLED', 200, 200,
                 '{"type":"MARKET","reduce_only":true}'::JSONB),
                (%s, %s, 'legacy-entry-2', NULL, 'BTCUSDT', 'SELL',
                 90, 1, 'FILLED', 200, 200,
                 '{"type":"LIMIT","reduce_only":false}'::JSONB),
                (%s, %s, 'legacy-exit-2', NULL, 'BTCUSDT', 'BUY',
                 NULL, 1, 'FILLED', 290, 300,
                 '{"type":"MARKET","reduce_only":true}'::JSONB)
            """,
            (research_id, run_id) * 5,
        )
        await connection.execute(
            """
            INSERT INTO backtest_fills (
                research_id, run_id, fill_id, order_id, symbol, side,
                price, quantity, commission, fill_time, payload
            ) VALUES
                (%s, %s, 'legacy-fill-entry-1', 'legacy-entry-filled',
                 'BTCUSDT', 'SELL', 100, 1, 0, 100, '{}'::JSONB),
                (%s, %s, 'legacy-fill-exit-1', 'legacy-exit-1',
                 'BTCUSDT', 'BUY', 90, 1, 0, 200, '{}'::JSONB),
                (%s, %s, 'legacy-fill-entry-2', 'legacy-entry-2',
                 'BTCUSDT', 'SELL', 90, 1, 0, 200, '{}'::JSONB),
                (%s, %s, 'legacy-fill-exit-2', 'legacy-exit-2',
                 'BTCUSDT', 'BUY', 80, 1, 0, 300, '{}'::JSONB)
            """,
            (research_id, run_id) * 4,
        )
    return research_id, first_trade_id, second_trade_id


@pytest.mark.asyncio
async def test_open_trade_execution_records_and_limit_fill_rate(backtest_repository):
    repository, pool = backtest_repository
    research_id, trade_id = await _seed_open_trade(pool)

    trade = await repository.get_trade(research_id, trade_id)
    assert trade is not None
    assert [order["order_id"] for order in trade["orders"]] == [
        "entry-1",
        "exit-1",
        "ordinary-limit-exit",
    ]
    assert {fill["order_id"] for fill in trade["fills"]} == {"entry-1", "exit-1"}
    assert trade["orders"][0]["filled_quantity"] == 1.0
    assert trade["orders"][0]["avg_fill_price"] == 99.0
    assert trade["orders"][1]["completed_time"] == 300

    rows, total = await repository.list_symbols(
        research_id,
        limit=10,
        offset=0,
        sort_by="limit_order_fill_rate",
    )
    assert total == 1
    assert rows[0]["limit_order_fill_rate"] == 1.0


@pytest.mark.asyncio
async def test_legacy_trade_windows_keep_sibling_orders_without_cross_linking(
    backtest_repository,
):
    repository, pool = backtest_repository
    research_id, first_trade_id, second_trade_id = await _seed_legacy_trade_windows(
        pool
    )

    first = await repository.get_trade(research_id, first_trade_id)
    second = await repository.get_trade(research_id, second_trade_id)

    assert first is not None
    assert [order["order_id"] for order in first["orders"]] == [
        "legacy-entry-filled",
        "legacy-entry-unfilled",
        "legacy-exit-1",
    ]
    assert second is not None
    assert [order["order_id"] for order in second["orders"]] == [
        "legacy-entry-2",
        "legacy-exit-2",
    ]
    assert {fill["order_id"] for fill in second["fills"]} == {
        "legacy-entry-2",
        "legacy-exit-2",
    }

    rows, _ = await repository.list_symbols(
        research_id,
        limit=10,
        offset=0,
        sort_by="limit_order_fill_rate",
    )
    assert rows[0]["limit_order_fill_rate"] == pytest.approx(2 / 3)
