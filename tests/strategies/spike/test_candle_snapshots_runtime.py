from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from trading_platform.ledger.db.models import CampaignCandleSnapshotManifest
from trading_platform.shared.events import Bar1s
from trading_platform.strategies.spike.candle_snapshots import (
    LiveCandleSnapshotRuntime,
    SnapshotRuntimeError,
)


def _bar(timestamp: int) -> Bar1s:
    return Bar1s(
        symbol="BTCUSDT",
        timestamp=timestamp,
        available_time=timestamp + 1_000,
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100"),
        volume=Decimal("1"),
        trade_count=1,
        vwap=Decimal("100"),
        first_aggregate_trade_id=timestamp // 1_000,
        last_aggregate_trade_id=timestamp // 1_000,
    )


class MemorySnapshotDB:
    def __init__(self) -> None:
        self.rows: dict[str, CampaignCandleSnapshotManifest] = {}
        self.completed: list[str] = []

    async def create_campaign_snapshot(self, manifest):
        self.rows.setdefault(manifest.snapshot_id, manifest)
        return self.rows[manifest.snapshot_id]

    async def complete_campaign_snapshot(self, snapshot_id, **kwargs):
        stored = self.rows[snapshot_id]
        self.rows[snapshot_id] = CampaignCandleSnapshotManifest(
            **{
                **stored.__dict__,
                "status": "completed",
                "parquet_relative_path": kwargs["parquet_relative_path"],
                "parquet_sha256": kwargs["parquet_sha256"],
                "row_count": kwargs["row_count"],
                "coverage": kwargs["coverage"],
                "gaps": kwargs["gaps"],
            }
        )
        self.completed.append(snapshot_id)
        return self.rows[snapshot_id]

    async def fail_campaign_snapshot(self, snapshot_id, *, reason, **_kwargs):
        stored = self.rows[snapshot_id]
        self.rows[snapshot_id] = CampaignCandleSnapshotManifest(
            **{**stored.__dict__, "status": "failed", "failure_reason": reason}
        )
        return self.rows[snapshot_id]


@pytest.mark.asyncio
async def test_runtime_keeps_short_1s_window_and_completes_manifest(tmp_path):
    db = MemorySnapshotDB()
    runtime = LiveCandleSnapshotRuntime(
        root=tmp_path,
        db=db,
        account_id="acct-a",
        strategy_id="spike_short",
        run_id="run-a",
        release_hash="a" * 64,
        pre_window_ms=1_000,
        post_window_ms=2_000,
    )
    await runtime.start()
    await runtime.observe_bar(_bar(1_000))
    snapshot_id = await runtime.ensure_signal_snapshot(
        campaign_id="spike_short:BTCUSDT:2000",
        symbol="BTCUSDT",
        signal_time_ms=2_000,
    )
    await runtime.observe_bar(_bar(2_000))
    await runtime.observe_bar(_bar(3_000))
    await asyncio.sleep(0.05)
    assert db.completed == [snapshot_id]
    assert not tuple((tmp_path / ".outbox").glob("*.json"))
    await runtime.close(now_ms=5_000)


def test_snapshot_id_is_account_and_strategy_scoped():
    first = LiveCandleSnapshotRuntime.snapshot_id_for_campaign(
        "acct-a", "spike_short", "spike_short:BTCUSDT:2000"
    )
    second = LiveCandleSnapshotRuntime.snapshot_id_for_campaign(
        "acct-b", "spike_short", "spike_short:BTCUSDT:2000"
    )
    assert first != second


@pytest.mark.asyncio
async def test_start_failure_fails_closed_and_does_not_return_signal(tmp_path):
    class FailingDB(MemorySnapshotDB):
        async def create_campaign_snapshot(self, manifest):
            raise RuntimeError("postgres unavailable")

    runtime = LiveCandleSnapshotRuntime(
        root=tmp_path,
        db=FailingDB(),
        account_id="acct-a",
        strategy_id="spike_short",
        run_id="run-a",
        release_hash="a" * 64,
        pre_window_ms=1_000,
        post_window_ms=2_000,
    )
    with pytest.raises(SnapshotRuntimeError):
        await runtime.ensure_signal_snapshot(
            campaign_id="spike_short:BTCUSDT:2000",
            symbol="BTCUSDT",
            signal_time_ms=2_000,
        )
    assert runtime.failed
