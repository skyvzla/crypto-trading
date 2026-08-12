from uuid import UUID, uuid4
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from trading_platform.ledger.api import backtests
from trading_platform.ledger.api.backtests import get_repository, router


class FakeBacktestRepository:
    def __init__(self) -> None:
        self.research_id = uuid4()
        self.trade_id = uuid4()
        self.last_trade_filters = {}

    async def list_researches(self, *, limit, offset):
        return ([{"id": self.research_id, "name": "July", "strategy_id": "spike_short"}], 1)

    async def get_research(self, research_id):
        if research_id != self.research_id:
            return None
        return {"id": research_id, "name": "July", "config": {}}

    async def has_symbol(self, research_id, symbol):
        return symbol == "AKEUSDT"

    async def list_reports(self, research_id):
        return [{"report_type": "parameter_summary", "title": "参数汇总", "columns": [], "row_count": 1}]

    async def get_report(self, research_id, report_type, *, limit, offset, sort_by=None, sort_order="desc"):
        if report_type == "missing":
            return None, []
        return ({"report_type": report_type, "columns": [{"key": "net_pnl"}], "row_count": 1}, [{"net_pnl": 12.5}])

    async def list_symbols(self, research_id, *, limit, offset, symbol_filter=None, sort_by="net_pnl", sort_order="desc"):
        return ([{"symbol": "AKEUSDT", "trade_count": 2}], 1)

    async def list_trades(self, research_id, symbol, **kwargs):
        self.last_trade_filters = kwargs
        return ([{"id": self.trade_id, "symbol": symbol, "net_pnl": -10}], 1)

    async def get_trade(self, research_id, trade_id):
        if trade_id != self.trade_id:
            return None
        return {"id": trade_id, "symbol": "AKEUSDT", "strategy_data": {"invalid_price": 1.2}}

    async def list_events(self, research_id, trade_id):
        if trade_id != self.trade_id:
            return None
        return [{"time": 1, "type": "signal_triggered", "data": {}}]

    async def get_strategy_schema(self, strategy_id):
        return None


@pytest.fixture
def api_app():
    app = FastAPI()
    app.include_router(router)
    repository = FakeBacktestRepository()
    app.dependency_overrides[get_repository] = lambda: repository
    return app, repository


@pytest.mark.asyncio
async def test_backtest_navigation_endpoints_follow_linear_hierarchy(api_app):
    app, repository = api_app
    transport = httpx.ASGITransport(app=app)
    base = f"/api/v1/backtest-researches/{repository.research_id}"
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        researches = await client.get("/api/v1/backtest-researches")
        reports = await client.get(f"{base}/reports")
        report = await client.get(f"{base}/reports/parameter_summary")
        symbols = await client.get(f"{base}/symbols")
        trades = await client.get(
            f"{base}/symbols/akeusdt/trades",
            params={"winner": False, "min_pnl": -100, "sort_by": "net_pnl", "sort_order": "asc"},
        )
        trade = await client.get(f"{base}/trades/{repository.trade_id}")
        events = await client.get(f"{base}/trades/{repository.trade_id}/events")

    assert researches.json()["total"] == 1
    assert reports.json()["items"][0]["report_type"] == "parameter_summary"
    assert report.json()["rows"] == [{"net_pnl": 12.5}]
    assert symbols.json()["items"][0]["symbol"] == "AKEUSDT"
    assert trades.json()["items"][0]["symbol"] == "AKEUSDT"
    assert repository.last_trade_filters["winner"] is False
    assert repository.last_trade_filters["min_pnl"] == -100
    assert repository.last_trade_filters["sort_by"] == "net_pnl"
    assert repository.last_trade_filters["sort_order"] == "asc"
    assert trade.json()["strategy_data"]["invalid_price"] == 1.2
    assert events.json()["items"][0]["type"] == "signal_triggered"


@pytest.mark.asyncio
async def test_missing_backtest_resources_are_404(api_app):
    app, repository = api_app
    transport = httpx.ASGITransport(app=app)
    base = f"/api/v1/backtest-researches/{repository.research_id}"
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        missing_report = await client.get(f"{base}/reports/missing")
        missing_trade = await client.get(f"{base}/trades/{uuid4()}")
        missing_schema = await client.get("/api/v1/backtest-strategies/unknown/schema")

    assert missing_report.status_code == 404
    assert missing_trade.status_code == 404
    assert missing_schema.status_code == 404


@pytest.mark.asyncio
async def test_archive_candles_fall_back_from_legacy_research_path(
    api_app, tmp_path, monkeypatch
):
    app, repository = api_app
    legacy_index = tmp_path / "history-parquet" / "archive_index.parquet"
    current_index = tmp_path / "candles" / "archive_index.parquet"
    current_index.parent.mkdir(parents=True)
    current_index.touch()
    repository.get_research = AsyncMock(return_value={
        "id": repository.research_id,
        "config": {"archive_index_path": str(legacy_index)},
        "source_metadata": {},
    })
    monkeypatch.setenv("BACKTEST_ARCHIVE_INDEX_PATH", str(legacy_index))
    monkeypatch.setattr(backtests, "DEFAULT_BACKTEST_ARCHIVE_INDEX_PATH", current_index)
    captured = {}

    def load_archive(index_path, symbol, interval, start_ms, end_ms):
        captured["index_path"] = index_path
        return []

    monkeypatch.setattr(backtests, "load_archive_candles", load_archive)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/v1/backtest-candles",
            params={
                "research_id": repository.research_id,
                "symbol": "AKEUSDT",
                "interval": "5m",
                "start_ms": 0,
                "end_ms": 300_000,
                "source": "archive",
            },
        )

    assert response.status_code == 200
    assert captured["index_path"] == current_index
