from datetime import datetime, timezone

import httpx
import pytest
from fastapi import FastAPI

from trading_platform.ledger.api.routes import router
from trading_platform.ledger.db.models import (
    ExchangeCategoryOverview,
    ExchangeSymbolOverview,
    ExchangeSymbolSyncState,
    StrategyCategoryAdmission,
    SymbolUniverseDecision,
)


MOMENT = datetime(2026, 8, 16, tzinfo=timezone.utc)


def _symbol() -> ExchangeSymbolOverview:
    return ExchangeSymbolOverview(
        symbol="BTCUSDT",
        pair="BTCUSDT",
        contract_type="PERPETUAL",
        status="TRADING",
        onboard_date=MOMENT,
        delivery_date=MOMENT,
        base_asset="BTC",
        quote_asset="USDT",
        margin_asset="USDT",
        underlying_type="COIN",
        raw_metadata={},
        active=True,
        synced_at=MOMENT,
        global_enabled=True,
        global_admission_version=0,
    )


class FakeLedger:
    def __init__(self):
        self.preview_kwargs = None
        self.symbol_kwargs = None
        self.category_kwargs = None
        self.strategy_page_kwargs = None

    async def list_exchange_symbols(
        self, limit, offset, *, unclassified=False
    ):
        self.symbol_kwargs = {
            "limit": limit,
            "offset": offset,
            "unclassified": unclassified,
        }
        return [_symbol()], 1

    async def list_exchange_categories(self, **_kwargs):
        self.category_kwargs = _kwargs
        return [
            ExchangeCategoryOverview(
                category_key="BINANCE:CATEGORY:COIN",
                source="BINANCE",
                category_type="CATEGORY",
                code="COIN",
                name="COIN",
                parent_key=None,
                active=True,
                synced_at=MOMENT,
                symbol_count=7,
            )
        ]

    async def count_exchange_categories(self, **_kwargs):
        return 7

    async def get_exchange_category(self, category_key):
        if category_key == "missing":
            return None
        return (await self.list_exchange_categories())[0]

    async def list_exchange_category_symbols(self, category_key, **_kwargs):
        assert category_key == "BINANCE:CATEGORY:COIN"
        return [_symbol()], 7

    async def get_exchange_symbol_sync_state(self):
        return ExchangeSymbolSyncState(
            status="SUCCESS",
            last_attempt_at=MOMENT,
            last_success_at=MOMENT,
            synced_symbols=500,
            last_error=None,
            stale=False,
            effective_universe_ready=True,
        )

    async def list_strategy_symbol_universe_preview(self, **kwargs):
        self.preview_kwargs = kwargs
        return (
            [
                SymbolUniverseDecision(
                    symbol="BTCUSDT",
                    sync_ready=True,
                    symbol_active=True,
                    perpetual_contract=True,
                    trading_status=True,
                    onboarded=True,
                    delivery_window_open=True,
                    global_enabled=False,
                    blocked_category_keys=["BINANCE:CATEGORY:COIN"],
                    effective=False,
                )
            ],
            20,
            12,
        )

    async def list_strategy_category_admissions(
        self, strategy_id, **kwargs
    ):
        self.strategy_page_kwargs = {"strategy_id": strategy_id, **kwargs}
        return [
            StrategyCategoryAdmission(
                strategy_id=strategy_id,
                category_key="BINANCE:CATEGORY:COIN",
                enabled=False,
                version=1,
                updated_at=MOMENT,
                updated_by="tester",
                reason="risk",
            )
        ]

    async def count_strategy_category_admissions(self, strategy_id):
        assert strategy_id == "spike_short"
        return 9

    async def set_strategy_category_admission(
        self,
        strategy_id,
        category_key,
        enabled,
        expected_version,
        updated_by,
        reason,
    ):
        assert expected_version == 0
        return StrategyCategoryAdmission(
            strategy_id=strategy_id,
            category_key=category_key,
            enabled=enabled,
            version=1,
            updated_at=MOMENT,
            updated_by=updated_by,
            reason=reason,
        )


@pytest.fixture
def ledger():
    return FakeLedger()


@pytest.fixture
def client(ledger):
    app = FastAPI()
    app.state.ledger_db = ledger
    app.include_router(router)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


@pytest.mark.asyncio
async def test_category_nodes_include_count_and_symbols_are_paged(client):
    async with client as http:
        categories = await http.get("/api/v1/exchange-categories")
        symbols = await http.get(
            "/api/v1/exchange-categories/BINANCE:CATEGORY:COIN/symbols",
            params={"limit": 10, "offset": 2},
        )
        missing = await http.get(
            "/api/v1/exchange-categories/missing/symbols"
        )
    assert categories.status_code == 200
    assert categories.json()[0]["symbol_count"] == 7
    assert symbols.status_code == 200
    assert symbols.json()["total"] == 7
    assert symbols.json()["limit"] == 10
    assert symbols.json()["offset"] == 2
    assert symbols.json()["items"][0]["symbol"] == "BTCUSDT"
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_category_and_strategy_policy_snapshots_offer_paged_endpoints(
    client, ledger
):
    async with client as http:
        categories = await http.get(
            "/api/v1/exchange-categories/page",
            params={"active_only": False, "limit": 20, "offset": 4},
        )
        strategy = await http.get(
            "/api/v1/strategy-category-admissions/spike_short/page",
            params={"limit": 25, "offset": 5},
        )

    assert categories.status_code == 200
    assert categories.json()["total"] == 7
    assert categories.json()["limit"] == 20
    assert categories.json()["offset"] == 4
    assert ledger.category_kwargs == {
        "active_only": False,
        "limit": 20,
        "offset": 4,
    }
    assert strategy.status_code == 200
    assert strategy.json()["total"] == 9
    assert strategy.json()["limit"] == 25
    assert strategy.json()["offset"] == 5
    assert ledger.strategy_page_kwargs == {
        "strategy_id": "spike_short",
        "limit": 25,
        "offset": 5,
    }


@pytest.mark.asyncio
async def test_exchange_symbols_supports_unclassified_filter(client, ledger):
    async with client as http:
        response = await http.get(
            "/api/v1/exchange-symbols",
            params={"unclassified": True, "limit": 20, "offset": 3},
        )

    assert response.status_code == 200
    assert response.json()["items"][0]["symbol"] == "BTCUSDT"
    assert ledger.symbol_kwargs == {
        "limit": 20,
        "offset": 3,
        "unclassified": True,
    }


@pytest.mark.asyncio
async def test_sync_status_exposes_readiness_without_mutation(client):
    async with client as http:
        response = await http.get("/api/v1/exchange-symbol-sync/status")
    assert response.status_code == 200
    assert response.json() == {
        "initialized": True,
        "status": "SUCCESS",
        "last_attempt_at": "2026-08-16T00:00:00Z",
        "last_success_at": "2026-08-16T00:00:00Z",
        "synced_symbols": 500,
        "last_error": None,
        "stale": False,
        "effective_universe_ready": True,
        "max_age_hours": 36,
    }


@pytest.mark.asyncio
async def test_strategy_preview_returns_backend_exclusion_reasons(
    client, ledger
):
    async with client as http:
        response = await http.get(
            "/api/v1/strategy-category-admissions/spike_short/universe-preview",
            params={
                "freeze_days": 20,
                "effective": False,
                "limit": 25,
                "offset": 5,
            },
        )
    assert response.status_code == 200
    assert response.json() == {
        "strategy_id": "spike_short",
        "freeze_days": 20,
        "total_symbols": 20,
        "effective_symbols": 12,
        "excluded_symbols": 8,
        "total": 8,
        "items": [
            {
                "symbol": "BTCUSDT",
                "effective": False,
                "exclusion_reasons": [
                    "GLOBAL_DISABLED",
                    "STRATEGY_CATEGORY_DISABLED",
                ],
                "blocked_category_keys": ["BINANCE:CATEGORY:COIN"],
            }
        ],
        "limit": 25,
        "offset": 5,
    }
    assert ledger.preview_kwargs == {
        "strategy_id": "spike_short",
        "freeze_days": 20,
        "effective": False,
        "limit": 25,
        "offset": 5,
    }


@pytest.mark.asyncio
async def test_strategy_category_admission_accepts_256_character_key(client):
    category_key = "x" * 256
    request = {
        "enabled": False,
        "expected_version": 0,
        "updated_by": "tester",
        "reason": "boundary",
    }
    async with client as http:
        accepted = await http.put(
            f"/api/v1/strategy-category-admissions/spike_short/{category_key}",
            json=request,
        )
        rejected = await http.put(
            "/api/v1/strategy-category-admissions/spike_short/" + "x" * 257,
            json=request,
        )

    assert accepted.status_code == 200
    assert accepted.json()["category_key"] == category_key
    assert rejected.status_code == 422
