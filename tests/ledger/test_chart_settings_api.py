"""Chart settings API integration tests backed by PostgreSQL."""

from __future__ import annotations

import os
import urllib.parse
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from psycopg import sql

from trading_platform.ledger.api.chart_settings import (
    DEFAULT_CHART_SETTINGS,
    default_chart_settings,
    router,
)
from trading_platform.ledger.db.migrations import apply_migrations
from trading_platform.ledger.db.models import LedgerDB, create_connection_pool


pytestmark = pytest.mark.skipif(
    not os.getenv("LEDGER_TEST_DSN"),
    reason="LEDGER_TEST_DSN not set",
)


@pytest.fixture
async def chart_settings_client():
    base_dsn = os.environ["LEDGER_TEST_DSN"]
    admin_pool = await create_connection_pool(base_dsn, 1, 2)
    schema = f"chart_settings_{uuid4().hex}"
    async with admin_pool.connection() as conn:
        await conn.execute(
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
    app = FastAPI()
    app.state.ledger_db = LedgerDB(pool)
    app.include_router(router)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield client, pool, schema
    finally:
        await pool.close()
        cleanup_pool = await create_connection_pool(base_dsn, 1, 2)
        async with cleanup_pool.connection() as conn:
            await conn.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
            )
        await cleanup_pool.close()


@pytest.mark.asyncio
async def test_get_returns_migration_defaults(chart_settings_client):
    client, _, _ = chart_settings_client

    response = await client.get("/api/v1/chart-settings")

    assert response.status_code == 200
    payload = response.json()
    assert payload["default_interval"] == "1s"
    assert payload["main"]["ema"] == DEFAULT_CHART_SETTINGS["main"]["ema"]
    assert payload["main"]["ma"]["lines"] == [
        {"period": 5, "color": "#f59e0b"},
        {"period": 10, "color": "#22c55e"},
        {"period": 20, "color": "#3b82f6"},
    ]
    assert payload["sub"]["volume"]["ma_lines"] == [
        {"period": 5, "color": "#f5c451"},
        {"period": 20, "color": "#4da3ff"},
    ]
    assert payload["updated_at"]


@pytest.mark.asyncio
async def test_get_adds_default_interval_to_legacy_document(chart_settings_client):
    client, pool, schema = chart_settings_client

    async with pool.connection() as conn:
        await conn.execute(
            sql.SQL(
                "UPDATE {}.chart_settings "
                "SET settings = settings - 'default_interval' "
                "WHERE setting_key = 'global'"
            ).format(sql.Identifier(schema))
        )

    response = await client.get("/api/v1/chart-settings")

    assert response.status_code == 200
    assert response.json()["default_interval"] == "1s"


@pytest.mark.asyncio
async def test_put_replaces_document_and_get_reads_it(chart_settings_client):
    client, pool, schema = chart_settings_client
    settings = default_chart_settings().model_dump(mode="json")
    settings["default_interval"] = "15m"
    settings["main"]["ma"] = {
        "enabled": True,
        "lines": [
            {"period": 3, "color": "#010203"},
            {"period": 55, "color": "#abcdef80"},
        ],
    }
    settings["sub"]["rsi"] = {
        "enabled": True,
        "lines": [{"period": 7, "color": "#112233"}],
    }

    response = await client.put("/api/v1/chart-settings", json=settings)
    assert response.status_code == 200
    assert response.json()["main"]["ma"] == settings["main"]["ma"]
    assert response.json()["sub"]["rsi"] == settings["sub"]["rsi"]
    assert response.json()["default_interval"] == "15m"

    loaded = await client.get("/api/v1/chart-settings")
    assert loaded.status_code == 200
    assert loaded.json()["main"]["ma"] == settings["main"]["ma"]
    assert loaded.json()["sub"]["rsi"] == settings["sub"]["rsi"]
    assert loaded.json()["default_interval"] == "15m"

    async with pool.connection() as conn:
        row = await (
            await conn.execute(
                sql.SQL(
                    "SELECT COUNT(*) OVER (), settings #>> '{{main,ma,lines,1,period}}' "
                    "FROM {}.chart_settings WHERE setting_key = 'global'"
                ).format(sql.Identifier(schema))
            )
        ).fetchone()
    assert row == (1, "55")


@pytest.mark.asyncio
async def test_put_rejects_invalid_period_color_and_unknown_fields(chart_settings_client):
    client, _, _ = chart_settings_client
    settings = default_chart_settings().model_dump(mode="json")

    invalid_color = settings.copy()
    invalid_color["main"] = {**settings["main"]}
    invalid_color["main"]["ema"] = {
        "enabled": True,
        "lines": [{"period": 9, "color": "red"}],
    }
    response = await client.put("/api/v1/chart-settings", json=invalid_color)
    assert response.status_code == 422

    duplicate_period = default_chart_settings().model_dump(mode="json")
    duplicate_period["main"]["ma"]["lines"] = [
        {"period": 5, "color": "#010203"},
        {"period": 5, "color": "#abcdef"},
    ]
    response = await client.put("/api/v1/chart-settings", json=duplicate_period)
    assert response.status_code == 422

    integer_deviation = default_chart_settings().model_dump(mode="json")
    integer_deviation["main"]["boll"]["deviation"] = 2
    response = await client.put("/api/v1/chart-settings", json=integer_deviation)
    assert response.status_code == 200

    valid_interval = default_chart_settings().model_dump(mode="json")
    valid_interval["default_interval"] = "15m"
    response = await client.put("/api/v1/chart-settings", json=valid_interval)
    assert response.status_code == 200
    assert response.json()["default_interval"] == "15m"

    invalid_interval = default_chart_settings().model_dump(mode="json")
    invalid_interval["default_interval"] = "30s"
    response = await client.put(
        "/api/v1/chart-settings", json=invalid_interval
    )
    assert response.status_code == 422

    invalid_period_order = settings.copy()
    invalid_period_order["sub"] = {**settings["sub"]}
    invalid_period_order["sub"]["macd"] = {
        **settings["sub"]["macd"],
        "fast_period": 26,
        "slow_period": 12,
    }
    response = await client.put(
        "/api/v1/chart-settings", json=invalid_period_order
    )
    assert response.status_code == 422

    unknown = settings.copy()
    unknown["main"] = {**settings["main"], "future_indicator": {}}
    response = await client.put("/api/v1/chart-settings", json=unknown)
    assert response.status_code == 422
