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
    assert payload["display"] == DEFAULT_CHART_SETTINGS["display"]
    assert payload["main"]["ema"] == DEFAULT_CHART_SETTINGS["main"]["ema"]
    assert payload["main"]["ma"]["lines"] == [
        {"period": 5, "color": "#f59e0b", "style": "solid", "width": 1},
        {"period": 10, "color": "#22c55e", "style": "solid", "width": 1},
        {"period": 20, "color": "#3b82f6", "style": "solid", "width": 1},
    ]
    assert payload["sub"]["volume"]["ma_lines"] == [
        {"period": 5, "color": "#f5c451", "style": "solid", "width": 1},
        {"period": 20, "color": "#4da3ff", "style": "solid", "width": 1},
    ]
    assert payload["updated_at"]


@pytest.mark.asyncio
async def test_get_enriches_legacy_document_with_display_and_line_defaults(
    chart_settings_client,
):
    client, pool, schema = chart_settings_client

    async with pool.connection() as conn:
        await conn.execute(
            sql.SQL(
                "UPDATE {}.chart_settings "
                "SET settings = settings - 'default_interval' - 'display' "
                "WHERE setting_key = 'global'"
            ).format(sql.Identifier(schema))
        )

    response = await client.get("/api/v1/chart-settings")

    assert response.status_code == 200
    payload = response.json()
    assert payload["default_interval"] == "1s"
    assert payload["display"] == DEFAULT_CHART_SETTINGS["display"]
    assert payload["main"]["ema"]["lines"][0]["style"] == "solid"
    assert payload["main"]["ema"]["lines"][0]["width"] == 1
    assert payload["main"]["boll"]["lines"]["middle"] == {
        "style": "dashed",
        "width": 1,
    }


@pytest.mark.asyncio
async def test_put_replaces_document_and_get_reads_it(chart_settings_client):
    client, pool, schema = chart_settings_client
    settings = default_chart_settings().model_dump(mode="json")
    settings["default_interval"] = "15m"
    settings["main"]["ma"] = {
        "enabled": True,
        "lines": [
            {
                "period": 3,
                "color": "#010203",
                "style": "dotted",
                "width": 2,
            },
            {
                "period": 55,
                "color": "#abcdef80",
                "style": "dashed",
                "width": 4,
            },
        ],
    }
    settings["sub"]["rsi"] = {
        "enabled": True,
        "lines": [
            {
                "period": 7,
                "color": "#112233",
                "style": "solid",
                "width": 3,
            }
        ],
    }
    settings["display"]["default_bar_spacing"] = 12.5
    settings["display"]["price_lines"]["average"] = {
        "visible": False,
        "style": "dotted",
        "width": 2,
    }

    response = await client.put("/api/v1/chart-settings", json=settings)
    assert response.status_code == 200
    assert response.json()["main"]["ma"] == settings["main"]["ma"]
    assert response.json()["sub"]["rsi"] == settings["sub"]["rsi"]
    assert response.json()["default_interval"] == "15m"
    assert response.json()["display"] == settings["display"]

    loaded = await client.get("/api/v1/chart-settings")
    assert loaded.status_code == 200
    assert loaded.json()["main"]["ma"] == settings["main"]["ma"]
    assert loaded.json()["sub"]["rsi"] == settings["sub"]["rsi"]
    assert loaded.json()["default_interval"] == "15m"
    assert loaded.json()["display"] == settings["display"]

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

    invalid_style = default_chart_settings().model_dump(mode="json")
    invalid_style["main"]["ema"]["lines"][0]["style"] = "dash-dot"
    response = await client.put("/api/v1/chart-settings", json=invalid_style)
    assert response.status_code == 422

    invalid_width = default_chart_settings().model_dump(mode="json")
    invalid_width["display"]["price_lines"]["signal"]["width"] = 5
    response = await client.put("/api/v1/chart-settings", json=invalid_width)
    assert response.status_code == 422

    invalid_zoom = default_chart_settings().model_dump(mode="json")
    invalid_zoom["display"]["default_bar_spacing"] = 31
    response = await client.put(
        "/api/v1/chart-settings", json=invalid_zoom
    )
    assert response.status_code == 422

    invalid_zoom_step = default_chart_settings().model_dump(mode="json")
    invalid_zoom_step["display"]["default_bar_spacing"] = 8.25
    response = await client.put(
        "/api/v1/chart-settings", json=invalid_zoom_step
    )
    assert response.status_code == 422

    for valid_zoom in (2, 30):
        boundary_zoom = default_chart_settings().model_dump(mode="json")
        boundary_zoom["display"]["default_bar_spacing"] = valid_zoom
        response = await client.put(
            "/api/v1/chart-settings", json=boundary_zoom
        )
        assert response.status_code == 200
