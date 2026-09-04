from typing import Any
from uuid import UUID, uuid4
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from trading_platform.ledger.api import backtests
from trading_platform.ledger.api.backtests import get_repository, router
from trading_platform.ledger.db.backtest_repository import BacktestRepository


class _ScriptedCursor:
    def __init__(self, pool: "_ScriptedPool") -> None:
        self.pool = pool
        self.rows: list[dict[str, Any]] = []

    async def __aenter__(self) -> "_ScriptedCursor":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def execute(
        self, query: str, parameters: tuple[object, ...] = ()
    ) -> None:
        self.pool.calls.append((query, parameters))
        self.rows = self.pool.responses.pop(0)

    async def fetchall(self) -> list[dict[str, Any]]:
        return self.rows


class _ScriptedConnection:
    def __init__(self, pool: "_ScriptedPool") -> None:
        self.pool = pool

    async def __aenter__(self) -> "_ScriptedConnection":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    def cursor(self, **kwargs: object) -> _ScriptedCursor:
        return _ScriptedCursor(self.pool)


class _ScriptedPool:
    def __init__(self, responses: list[list[dict[str, Any]]]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def connection(self) -> _ScriptedConnection:
        return _ScriptedConnection(self)


class FakeBacktestRepository:
    def __init__(self) -> None:
        self.research_id = uuid4()
        self.trade_id = uuid4()
        self.last_trade_filters = {}
        self.last_replay_parameters = None

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

    async def list_replay_parameter_sets(self, research_id):
        return [{"parameters": {"lookback": 6}, "trade_count": 2, "net_pnl": 10}]

    async def list_replay_trades(self, research_id, parameters):
        self.last_replay_parameters = parameters
        return [{
            "id": self.trade_id,
            "run_id": "ake-lookback-6",
            "symbol": "AKEUSDT",
            "entry_time": 100,
            "exit_time": 200,
            "entry_notional": 1000,
            "gross_pnl": 12,
            "commission": 2,
            "net_return": 0.01,
            "parameters": parameters,
        }]

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
async def test_equity_replay_endpoints_return_parameter_set_trade_facts(api_app):
    app, repository = api_app
    transport = httpx.ASGITransport(app=app)
    base = f"/api/v1/backtest-researches/{repository.research_id}"
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        parameter_sets = await client.get(f"{base}/replay-parameter-sets")
        trades = await client.get(
            f"{base}/replay-trades", params={"parameters": '{"lookback": 6}'}
        )

    assert parameter_sets.status_code == 200
    assert parameter_sets.json()["items"] == [
        {"parameters": {"lookback": 6}, "trade_count": 2, "net_pnl": 10}
    ]
    assert trades.status_code == 200
    assert trades.json()["items"][0]["gross_pnl"] == 12
    assert repository.last_replay_parameters == {"lookback": 6}


@pytest.mark.asyncio
@pytest.mark.parametrize("parameters", ["not-json", "[]", "null"])
async def test_equity_replay_trades_reject_non_object_parameters(
    api_app, parameters
):
    app, repository = api_app
    transport = httpx.ASGITransport(app=app)
    path = (
        f"/api/v1/backtest-researches/{repository.research_id}"
        "/replay-trades"
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(path, params={"parameters": parameters})

    assert response.status_code == 422


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


@pytest.mark.asyncio
async def test_repository_get_trade_scopes_orders_and_fills_to_trade_lifecycle():
    research_id = uuid4()
    trade_id = uuid4()
    pool = _ScriptedPool(
        [
            [
                {
                    "id": trade_id,
                    "run_id": "run-1",
                    "campaign_id": "campaign-1",
                    "symbol": "BTCUSDT",
                    "side": "SHORT",
                    "entry_time": 100,
                    "exit_time": 300,
                    "entry_price": "100",
                    "exit_price": "90",
                    "net_pnl": "-10",
                    "net_return": "-0.1",
                    "strategy_data": {},
                }
            ],
            [
                {
                    "order_id": "entry-1",
                    "campaign_id": "campaign-1",
                    "symbol": "BTCUSDT",
                    "side": "SELL",
                    "price": "100",
                    "quantity": "3",
                    "status": "PARTIALLY_FILLED",
                    "created_at": 100,
                    "fill_time": 150,
                    "payload": {
                        "type": "LIMIT",
                        "client_order_id": "entry-client-1",
                        "account_id": "backtest",
                        "strategy_id": "spike_short",
                        "ttl_ms": 60_000,
                        "reduce_only": False,
                        "commission_asset": "USDT",
                        "is_maker": True,
                        "trigger_reason": "signal",
                        "tier": 1,
                    },
                },
                {
                    "order_id": "exit-1",
                    "campaign_id": None,
                    "symbol": "BTCUSDT",
                    "side": "BUY",
                    "price": None,
                    "quantity": "1.5",
                    "status": "FILLED",
                    "created_at": 250,
                    "fill_time": 260,
                    "payload": {
                        "type": "MARKET",
                        "client_order_id": "exit-client-1",
                        "reduce_only": True,
                    },
                },
            ],
            [
                {
                    "fill_id": "fill-1",
                    "order_id": "entry-1",
                    "symbol": "BTCUSDT",
                    "side": "SELL",
                    "price": "99",
                    "quantity": "1",
                    "commission": "0.1",
                    "fill_time": 150,
                    "payload": {"commission_asset": "USDT", "is_maker": True},
                },
                {
                    "fill_id": "fill-2",
                    "order_id": "entry-1",
                    "symbol": "BTCUSDT",
                    "side": "SELL",
                    "price": "98",
                    "quantity": "0.5",
                    "commission": "0.05",
                    "fill_time": 155,
                    "payload": {"commission_asset": "USDT", "is_maker": True},
                },
                {
                    "fill_id": "fill-exit-1",
                    "order_id": "exit-1",
                    "symbol": "BTCUSDT",
                    "side": "BUY",
                    "price": "90",
                    "quantity": "1.5",
                    "commission": "0.15",
                    "fill_time": 260,
                    "payload": {"commission_asset": "USDT", "is_maker": False},
                },
            ],
        ]
    )

    trade = await BacktestRepository(pool).get_trade(research_id, trade_id)

    assert trade is not None
    assert [order["order_id"] for order in trade["orders"]] == ["entry-1", "exit-1"]
    assert {fill["order_id"] for fill in trade["fills"]} == {"entry-1", "exit-1"}
    entry = trade["orders"][0]
    assert entry["type"] == "LIMIT"
    assert entry["account_id"] == "backtest"
    assert entry["strategy_id"] == "spike_short"
    assert entry["ttl_ms"] == 60_000
    assert entry["reduce_only"] is False
    assert entry["filled_quantity"] == 1.5
    assert entry["avg_fill_price"] == pytest.approx((99 + 98 * 0.5) / 1.5)
    assert entry["commission"] == pytest.approx(0.15)
    assert entry["completed_time"] is None
    assert trade["orders"][1]["completed_time"] == 260

    order_query, order_parameters = pool.calls[1]
    assert "campaign_id IS NULL" in order_query
    assert "reduce_only" in order_query
    assert "COALESCE(fill_time, created_at) >= COALESCE(%s::BIGINT, %s::BIGINT)" in order_query
    assert "%s::BIGINT IS NULL OR created_at <= %s::BIGINT" in order_query
    assert order_parameters == (
        research_id,
        "run-1",
        "BTCUSDT",
        "campaign-1",
        "campaign-1",
        "SELL",
        None,
        100,
        300,
        300,
    )
    fills_query, fills_parameters = pool.calls[2]
    assert "order_id = ANY(%s)" in fills_query
    assert fills_parameters == (research_id, "run-1", "BTCUSDT", ["entry-1", "exit-1"])


@pytest.mark.asyncio
async def test_repository_get_trade_returns_empty_execution_arrays_without_fill_query():
    research_id = uuid4()
    trade_id = uuid4()
    pool = _ScriptedPool(
        [
            [
                {
                    "id": trade_id,
                    "run_id": "legacy-run",
                    "campaign_id": None,
                    "symbol": "ETHUSDT",
                    "signal_time": None,
                    "entry_time": 500,
                    "exit_time": None,
                    "entry_price": "10",
                    "exit_price": None,
                    "net_pnl": "0",
                    "net_return": "0",
                    "strategy_data": {},
                }
            ],
            [],
        ]
    )

    trade = await BacktestRepository(pool).get_trade(research_id, trade_id)

    assert trade is not None
    assert trade["orders"] == []
    assert trade["fills"] == []
    assert len(pool.calls) == 2
    order_query, order_parameters = pool.calls[1]
    assert "%s::TEXT IS NULL" in order_query
    assert "UPPER(side) = UPPER(%s::TEXT)" in order_query
    assert "%s::BIGINT IS NULL OR created_at <= %s::BIGINT" in order_query
    assert order_parameters == (
        research_id,
        "legacy-run",
        "ETHUSDT",
        None,
        None,
        None,
        None,
        500,
        None,
        None,
    )


@pytest.mark.asyncio
async def test_repository_list_symbols_reports_limit_order_fill_rate_and_null_denominator():
    research_id = uuid4()
    pool = _ScriptedPool(
        [
            [
                {
                    "symbol": "BTCUSDT",
                    "trade_count": 2,
                    "win_count": 1,
                    "limit_order_fill_rate": "0.5",
                },
                {
                    "symbol": "ETHUSDT",
                    "trade_count": 1,
                    "win_count": 1,
                    "limit_order_fill_rate": None,
                },
            ],
            [{"count": 2}],
        ]
    )

    rows, total = await BacktestRepository(pool).list_symbols(
        research_id,
        limit=20,
        offset=0,
        sort_by="limit_order_fill_rate",
    )

    assert total == 2
    assert rows[0]["limit_order_fill_rate"] == 0.5
    assert rows[0]["win_rate"] == 0.5
    assert rows[1]["limit_order_fill_rate"] is None
    query, parameters = pool.calls[0]
    assert "UPPER(o.payload->>'type') = 'LIMIT'" in query
    assert "reduce_only" in query
    assert "EXISTS" in query
    assert "CASE UPPER(t.side)" in query
    assert "WHEN 'SHORT' THEN 'SELL'" in query
    assert "t.campaign_id IS NULL" in query
    assert "COALESCE(o.fill_time, o.created_at) >= t.entry_time" in query
    assert parameters == (research_id, "%", 20, 0)


@pytest.mark.asyncio
async def test_repository_list_trades_exposes_entry_fill_count_and_uses_it_for_sorting():
    research_id = uuid4()
    pool = _ScriptedPool(
        [
            [{"trade_id": "trade-1", "entry_fill_count": 2}],
            [{"count": 1}],
        ]
    )

    rows, total = await BacktestRepository(pool).list_trades(
        research_id,
        "BTCUSDT",
        limit=10,
        offset=20,
        sort_by="entry_fill_count",
        sort_order="asc",
    )

    assert total == 1
    assert rows == [{"trade_id": "trade-1", "entry_fill_count": 2}]
    query, parameters = pool.calls[0]
    assert "entry_fill_count," in query
    assert "ORDER BY entry_fill_count ASC" in query
    assert parameters == (research_id, research_id, "BTCUSDT", 10, 20)
    assert pool.calls[1][1] == (research_id, research_id, "BTCUSDT")
