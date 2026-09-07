from datetime import UTC, datetime
import hashlib
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from trading_platform.ledger.api import routes
from trading_platform.ledger.api.routes import get_db, router
from trading_platform.ledger.db.models import CampaignCandleSnapshotManifest


SHA256 = "a" * 64


def _manifest(
    *,
    status: str = "completed",
    relative_path: str | None = "snapshots/campaign.parquet",
) -> CampaignCandleSnapshotManifest:
    now = datetime(2026, 9, 7, tzinfo=UTC)
    return CampaignCandleSnapshotManifest(
        snapshot_id="snapshot-1",
        account_id="acct-1",
        strategy_id="spike_short",
        campaign_id="campaign-1",
        symbol="BTCUSDT",
        run_id="run-1",
        signal_time_ms=1_000,
        window_start_ms=0,
        window_end_ms=2_000,
        status=status,
        coverage={"expected_rows": 2, "present_rows": 2},
        gaps=[],
        parquet_relative_path=relative_path,
        parquet_sha256=SHA256 if relative_path else None,
        row_count=2 if relative_path else None,
        schema_version=2,
        aggregation_version=3,
        release_hash="b" * 64,
        failure_reason="writer stopped" if status == "failed" else None,
        created_at=now,
        updated_at=now,
        completed_at=now if status == "completed" else None,
        failed_at=now if status == "failed" else None,
    )


class FakeDB:
    def __init__(self, manifest: CampaignCandleSnapshotManifest | None) -> None:
        self.manifest = manifest
        self.calls: list[dict[str, str]] = []

    async def get_campaign_snapshot(self, **filters: str):
        self.calls.append(filters)
        return self.manifest


@pytest.mark.asyncio
async def test_campaign_snapshot_reader_adapter_uses_market_helper(
    api_app, monkeypatch: pytest.MonkeyPatch
):
    app, _database, payload = api_app
    captured = {}

    async def read_snapshot(path, *, symbol, interval, start_ms, end_ms):
        captured.update(
            path=path,
            symbol=symbol,
            interval=interval,
            start_ms=start_ms,
            end_ms=end_ms,
        )
        return []

    monkeypatch.setattr(routes, "_read_campaign_snapshot_candles", read_snapshot)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/v1/campaigns/campaign-1/candles",
            params={
                "account_id": "acct-1",
                "strategy_id": "spike_short",
                "symbol": "BTCUSDT",
            },
        )
    assert response.status_code == 200
    assert captured == {
        "path": payload,
        "symbol": "BTCUSDT",
        "interval": "1s",
        "start_ms": None,
        "end_ms": None,
    }


@pytest.fixture
def api_app(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    app = FastAPI()
    app.include_router(router)
    database = FakeDB(_manifest())
    app.dependency_overrides[get_db] = lambda: database
    root = tmp_path / "snapshots"
    payload = root / "snapshots" / "campaign.parquet"
    payload.parent.mkdir(parents=True)
    payload.write_bytes(b"snapshot")
    database.manifest.parquet_sha256 = hashlib.sha256(payload.read_bytes()).hexdigest()
    monkeypatch.setenv("CAMPAIGN_SNAPSHOT_ROOT", str(root))
    return app, database, payload


@pytest.mark.asyncio
async def test_campaign_snapshot_candles_return_extended_1s_fields(
    api_app, monkeypatch: pytest.MonkeyPatch
):
    app, database, payload = api_app

    async def read_snapshot(path, *, symbol, interval, start_ms, end_ms):
        assert path == payload
        assert symbol == "BTCUSDT"
        assert interval == "1s"
        assert start_ms == 100
        assert end_ms == 900
        return [
            {
                "time": 1000,
                "open": 10.0,
                "close": 11.0,
                "volume": 5.0,
                "quote_volume": 55.0,
                "taker_buy_quote_volume": 40.0,
                "taker_sell_quote_volume": 15.0,
                "trade_count": 12,
            }
        ]

    monkeypatch.setattr(routes, "_read_campaign_snapshot_candles", read_snapshot)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/v1/campaigns/campaign-1/candles",
            params={
                "account_id": "acct-1",
                "strategy_id": "spike_short",
                "symbol": "btcusdt",
                "interval": "1s",
                "start_ms": 100,
                "end_ms": 900,
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["interval"] == "1s"
    assert body["source"] == "campaign_snapshot"
    assert body["candles"][0]["taker_buy_quote_volume"] == 40.0
    assert body["snapshot"]["aggregation_version"] == 3
    assert body["snapshot"]["release_hash"] == "b" * 64
    assert database.calls == [
        {
            "account_id": "acct-1",
            "strategy_id": "spike_short",
            "symbol": "BTCUSDT",
            "campaign_id": "campaign-1",
        }
    ]


@pytest.mark.asyncio
async def test_campaign_snapshot_api_is_strictly_1s(api_app):
    app, _database, _payload = api_app
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/v1/campaigns/campaign-1/candles",
            params={
                "account_id": "acct-1",
                "strategy_id": "spike_short",
                "symbol": "BTCUSDT",
                "interval": "1m",
            },
        )
    assert response.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected_code"),
    [("collecting", 409), ("failed", 409)],
)
async def test_campaign_snapshot_api_exposes_unavailable_status(
    api_app, status: str, expected_code: int
):
    app, database, _payload = api_app
    database.manifest = _manifest(status=status, relative_path=None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/v1/campaigns/campaign-1/candles",
            params={
                "account_id": "acct-1",
                "strategy_id": "spike_short",
                "symbol": "BTCUSDT",
            },
        )
    assert response.status_code == expected_code


@pytest.mark.asyncio
async def test_campaign_snapshot_api_rejects_path_escape(api_app):
    app, database, _payload = api_app
    database.manifest = _manifest(relative_path="../outside.parquet")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/v1/campaigns/campaign-1/candles",
            params={
                "account_id": "acct-1",
                "strategy_id": "spike_short",
                "symbol": "BTCUSDT",
            },
        )
    assert response.status_code == 500
    assert "escapes" in response.json()["detail"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("coverage", "expected_status"),
    [
        (
            {
                "expected_rows": 5,
                "present_rows": 1,
                "complete": True,
                "continuity_ok": True,
            },
            "complete",
        ),
        (
            {
                "expected_rows": 5,
                "present_rows": 5,
                "complete": False,
                "continuity_ok": True,
            },
            "incomplete",
        ),
        (
            {
                "coverage_expected": 5,
                "coverage_received": 4,
                "complete": True,
                "continuity_ok": True,
            },
            "incomplete",
        ),
        (
            {
                "expected_rows": 5,
                "present_rows": 5,
                "complete": True,
                "continuity_ok": False,
            },
            "incomplete",
        ),
    ],
)
async def test_campaign_snapshot_api_uses_coverage_semantics(
    api_app, monkeypatch: pytest.MonkeyPatch, coverage, expected_status
):
    app, database, _payload = api_app
    database.manifest.coverage = coverage

    async def read_snapshot(*args, **kwargs):
        return []

    monkeypatch.setattr(routes, "_read_campaign_snapshot_candles", read_snapshot)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/v1/campaigns/campaign-1/candles",
            params={
                "account_id": "acct-1",
                "strategy_id": "spike_short",
                "symbol": "BTCUSDT",
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["coverage_status"] == expected_status
