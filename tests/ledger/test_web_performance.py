from datetime import date, datetime, timezone
from decimal import Decimal

import httpx
import pytest
from fastapi import FastAPI

from trading_platform.ledger.api.routes import router
from trading_platform.ledger.db.models import DailyPnLFact, PerformanceCampaignFact


class FakeLedger:
    async def list_daily_realized_pnl(self, **_kwargs):
        return [
            DailyPnLFact(
                day=date(2026, 8, 1),
                trade_count=3,
                realized_trade_count=1,
                gross_realized_pnl=Decimal("12.5"),
                total_commission=Decimal("0.5"),
                commission_asset="USDT",
                net_pnl=Decimal("12.0"),
            )
        ]

    async def list_performance_campaign_facts(self, **_kwargs):
        moment = datetime(2026, 8, 1, tzinfo=timezone.utc)
        return [
            PerformanceCampaignFact(
                account_id="a",
                strategy_id="s",
                symbol="BTCUSDT",
                campaign_id="win",
                trade_count=2,
                total_commission=Decimal("2"),
                gross_realized_pnl=Decimal("12"),
                sell_quantity=Decimal("1"),
                buy_quantity=Decimal("1"),
                commission_asset="USDT",
                realized_pnl_complete=True,
                unique_symbols=1,
                first_fill_at=moment,
                last_fill_at=moment,
                closed_at=moment,
            ),
            PerformanceCampaignFact(
                account_id="a",
                strategy_id="s",
                symbol="BTCUSDT",
                campaign_id="loss",
                trade_count=2,
                total_commission=Decimal("1"),
                gross_realized_pnl=Decimal("-4"),
                sell_quantity=Decimal("1"),
                buy_quantity=Decimal("1"),
                commission_asset="USDT",
                realized_pnl_complete=True,
                unique_symbols=1,
                first_fill_at=moment,
                last_fill_at=moment,
                closed_at=moment,
            ),
            PerformanceCampaignFact(
                account_id="a",
                strategy_id="s",
                symbol="BTCUSDT",
                campaign_id="incomplete",
                trade_count=1,
                total_commission=Decimal("0.1"),
                gross_realized_pnl=Decimal("3"),
                sell_quantity=Decimal("1"),
                buy_quantity=Decimal("0"),
                commission_asset="USDT",
                realized_pnl_complete=True,
                unique_symbols=1,
                first_fill_at=moment,
                last_fill_at=moment,
                closed_at=None,
            ),
        ]

    async def count_unattributed_trades(self, **_kwargs):
        return 4


@pytest.fixture
def client():
    app = FastAPI()
    app.state.ledger_db = FakeLedger()
    app.include_router(router)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


@pytest.mark.asyncio
async def test_daily_pnl_is_a_utc_calendar_aggregate(client):
    async with client as http:
        response = await http.get(
            "/api/v1/pnl/daily",
            params={
                "account_id": "a",
                "start_date": "2026-08-01",
                "end_date": "2026-08-31",
            },
        )
    assert response.status_code == 200
    assert response.json() == [
        {
            "date": "2026-08-01",
            "account_id": "a",
            "strategy_id": None,
            "symbol": None,
            "trade_count": 3,
            "realized_trade_count": 1,
            "gross_realized_pnl": "12.5",
            "total_commission": "0.5",
            "commission_asset": "USDT",
            "net_pnl": "12.0",
        }
    ]


@pytest.mark.asyncio
async def test_performance_metrics_exclude_open_campaigns(client):
    async with client as http:
        response = await http.get(
            "/api/v1/performance",
            params={
                "account_id": "a",
                "strategy_id": "s",
                "start_date": "2026-08-01",
                "end_date": "2026-08-01",
            },
        )
    assert response.status_code == 200
    payload = response.json()
    assert payload["total_trades"] == 2
    assert payload["total_fills"] == 4
    assert payload["win_count"] == 1
    assert payload["loss_count"] == 1
    assert payload["win_rate"] == 0.5
    assert Decimal(payload["avg_win"]) == Decimal("10")
    assert Decimal(payload["avg_loss"]) == Decimal("5")
    assert Decimal(payload["payoff_ratio"]) == Decimal("2")
    assert Decimal(payload["expectancy"]) == Decimal("2.5")
    assert Decimal(payload["profit_factor"]) == Decimal("2")
    assert Decimal(payload["max_drawdown"]) == Decimal("-5")
    assert payload["candidate_campaigns"] == 3
    assert payload["excluded_campaigns"] == 1
    assert payload["unattributed_fills"] == 4
