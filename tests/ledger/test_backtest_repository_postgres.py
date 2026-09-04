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
