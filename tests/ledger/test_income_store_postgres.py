"""IncomeStore integration tests against an isolated real PostgreSQL schema."""

import os
import urllib.parse
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import AsyncMock

import pytest

from trading_platform.ledger.db.migrations import apply_migrations
from trading_platform.ledger.db.models import create_connection_pool
from trading_platform.ledger.income_store import IncomeStore
from trading_platform.ledger.income_sync import FundingIncomeSync


pytestmark = pytest.mark.skipif(
    not os.getenv("LEDGER_TEST_DSN"), reason="LEDGER_TEST_DSN not set"
)


@pytest.fixture
async def income_store():
    base_dsn = os.environ["LEDGER_TEST_DSN"]
    schema = f"income_{uuid4().hex}"
    admin_pool = await create_connection_pool(base_dsn, 1, 2)
    async with admin_pool.connection() as connection:
        await connection.execute(f'CREATE SCHEMA "{schema}"')
    await admin_pool.close()

    dsn = base_dsn + "?options=" + urllib.parse.quote(f"-csearch_path={schema}")
    pool = await create_connection_pool(dsn, 1, 4)
    await apply_migrations(pool, schema=schema)
    try:
        yield IncomeStore(pool)
    finally:
        await pool.close()
        cleanup_pool = await create_connection_pool(base_dsn, 1, 2)
        async with cleanup_pool.connection() as connection:
            await connection.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await cleanup_pool.close()


@pytest.mark.asyncio
async def test_income_history_can_be_persisted_and_funding_fee_summed(income_store):
    occurred_at = datetime(2026, 8, 20, 8, tzinfo=UTC)
    processed = await income_store.upsert_income_history(
        account_id="spike-testnet",
        rows=[
            {
                "symbol": "BTCUSDT",
                "incomeType": "FUNDING_FEE",
                "income": "-0.125",
                "asset": "USDT",
                "time": int(occurred_at.timestamp() * 1000),
                "tranId": 123456789,
                "info": "",
            }
        ],
    )

    total = await income_store.funding_fee_total(
        account_id="spike-testnet",
        symbol="BTCUSDT",
        start_at=occurred_at - timedelta(seconds=1),
        end_at=occurred_at + timedelta(seconds=1),
    )

    assert processed == 1
    assert total == Decimal("-0.125")
    async with income_store.pool.connection() as connection:
        stored = await (
            await connection.execute(
                "SELECT transaction_id, income_type, symbol, asset, amount, "
                "event_time, raw FROM account_income_events "
                "WHERE account_id = %s",
                ("spike-testnet",),
            )
        ).fetchone()
    assert stored == (
        123456789,
        "FUNDING_FEE",
        "BTCUSDT",
        "USDT",
        Decimal("-0.125"),
        occurred_at,
        {
            "symbol": "BTCUSDT",
            "incomeType": "FUNDING_FEE",
            "income": "-0.125",
            "asset": "USDT",
            "time": int(occurred_at.timestamp() * 1000),
            "tranId": 123456789,
            "info": "",
        },
    )


@pytest.mark.asyncio
async def test_replayed_income_row_is_idempotent(income_store):
    occurred_at = datetime(2026, 8, 20, 8, tzinfo=UTC)
    row = {
        "symbol": "BTCUSDT",
        "incomeType": "FUNDING_FEE",
        "income": "-0.125",
        "asset": "USDT",
        "time": int(occurred_at.timestamp() * 1000),
        "tranId": 223456789,
    }

    assert await income_store.upsert_income_history(
        account_id="spike-idempotent", rows=[row]
    ) == 1
    assert await income_store.upsert_income_history(
        account_id="spike-idempotent", rows=[row]
    ) == 1

    assert await income_store.funding_fee_total(
        account_id="spike-idempotent",
        symbol="BTCUSDT",
        start_at=occurred_at - timedelta(seconds=1),
        end_at=occurred_at + timedelta(seconds=1),
    ) == Decimal("-0.125")


@pytest.mark.asyncio
async def test_funding_total_filters_type_symbol_and_half_open_window(income_store):
    start_at = datetime(2026, 8, 20, 8, tzinfo=UTC)
    end_at = start_at + timedelta(hours=8)

    def row(
        tran_id: int,
        *,
        moment: datetime,
        symbol: str = "BTCUSDT",
        income_type: str = "FUNDING_FEE",
        amount: str = "-0.1",
    ) -> dict[str, object]:
        return {
            "symbol": symbol,
            "incomeType": income_type,
            "income": amount,
            "asset": "USDT",
            "time": int(moment.timestamp() * 1000),
            "tranId": tran_id,
        }

    await income_store.upsert_income_history(
        account_id="spike-filter",
        rows=[
            row(1, moment=start_at, amount="-0.2"),
            row(2, moment=end_at, amount="-9"),
            row(3, moment=start_at, symbol="ETHUSDT", amount="-8"),
            row(4, moment=start_at, income_type="COMMISSION", amount="-7"),
        ],
    )

    assert await income_store.funding_fee_total(
        account_id="spike-filter",
        symbol="BTCUSDT",
        start_at=start_at,
        end_at=end_at,
    ) == Decimal("-0.2")


@pytest.mark.asyncio
async def test_invalid_income_page_is_rejected_before_any_row_is_written(income_store):
    occurred_at = datetime(2026, 8, 20, 8, tzinfo=UTC)
    valid = {
        "symbol": "BTCUSDT",
        "incomeType": "FUNDING_FEE",
        "income": "-0.1",
        "asset": "USDT",
        "time": int(occurred_at.timestamp() * 1000),
        "tranId": 10,
    }
    invalid = {**valid, "tranId": 11, "income": "not-a-number"}

    with pytest.raises(ValueError, match="invalid Binance income row"):
        await income_store.upsert_income_history(
            account_id="spike-invalid", rows=[valid, invalid]
        )

    assert await income_store.funding_fee_total(
        account_id="spike-invalid",
        symbol="BTCUSDT",
        start_at=occurred_at - timedelta(seconds=1),
        end_at=occurred_at + timedelta(seconds=1),
    ) == Decimal("0")


@pytest.mark.asyncio
async def test_complete_sync_chain_persists_rest_page_before_aggregation(income_store):
    start_at = datetime(2026, 8, 20, 8, tzinfo=UTC)
    event_at = start_at + timedelta(hours=1)
    client = SimpleNamespace(
        get_income_history=AsyncMock(
            return_value=[
                {
                    "symbol": "BTCUSDT",
                    "incomeType": "FUNDING_FEE",
                    "income": "0.75",
                    "asset": "USDT",
                    "time": int(event_at.timestamp() * 1000),
                    "tranId": 99887766,
                }
            ]
        )
    )

    total = await FundingIncomeSync(client, income_store).sync_funding_fee_total(
        account_id="spike-chain",
        symbol="BTCUSDT",
        start_at=start_at,
        end_at=start_at + timedelta(hours=2),
    )

    assert total == Decimal("0.75")
    assert client.get_income_history.await_count == 1
