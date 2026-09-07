"""PostgreSQL contract tests for campaign candle snapshot manifests."""

import os
from uuid import uuid4

import pytest

from trading_platform.ledger.db.migrations import apply_migrations, verify_current
from trading_platform.ledger.db.models import (
    CampaignCandleSnapshotManifest,
    LedgerDB,
    create_connection_pool,
)


pytestmark = pytest.mark.skipif(
    not os.getenv("LEDGER_TEST_DSN"),
    reason="LEDGER_TEST_DSN not set",
)


@pytest.fixture
async def ledger():
    pool = await create_connection_pool(os.environ["LEDGER_TEST_DSN"], 1, 4)
    await apply_migrations(pool)
    await verify_current(pool)
    try:
        yield LedgerDB(pool)
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_snapshot_manifest_lifecycle_is_idempotent_and_account_scoped(ledger):
    snapshot_id = f"snapshot-{uuid4().hex}"
    manifest = CampaignCandleSnapshotManifest(
        snapshot_id=snapshot_id,
        account_id="snapshot-account",
        strategy_id="spike_short",
        campaign_id=f"campaign-{uuid4().hex}",
        symbol="btcusdt",
        run_id="run-1",
        signal_time_ms=1_000,
        window_start_ms=0,
        window_end_ms=2_000,
        release_hash="a" * 64,
        schema_version=2,
        aggregation_version=3,
    )

    created = await ledger.create_campaign_snapshot(manifest)
    retried = await ledger.create_campaign_snapshot(manifest)
    assert created.status == "collecting"
    assert retried.snapshot_id == snapshot_id
    assert retried.symbol == "BTCUSDT"

    completed = await ledger.complete_campaign_snapshot(
        snapshot_id,
        parquet_relative_path="BTCUSDT/snapshot.parquet",
        parquet_sha256="b" * 64,
        row_count=2,
        coverage={"expected_count": 2, "received_count": 2},
        gaps=[],
    )
    assert completed.status == "completed"
    assert completed.parquet_relative_path == "BTCUSDT/snapshot.parquet"
    assert completed.row_count == 2
    assert completed.release_hash == "a" * 64

    retried_completion = await ledger.complete_campaign_snapshot(
        snapshot_id,
        parquet_relative_path="BTCUSDT/snapshot.parquet",
        parquet_sha256="b" * 64,
        row_count=2,
        coverage={"expected_count": 2, "received_count": 2},
        gaps=[],
    )
    assert retried_completion.snapshot_id == snapshot_id
    assert retried_completion.completed_at == completed.completed_at

    selected = await ledger.get_campaign_snapshot(
        account_id=manifest.account_id,
        strategy_id=manifest.strategy_id,
        symbol=manifest.symbol,
        campaign_id=manifest.campaign_id,
    )
    assert selected is not None
    assert selected.snapshot_id == snapshot_id

    with pytest.raises(ValueError, match="completed payload conflicts"):
        await ledger.complete_campaign_snapshot(
            snapshot_id,
            parquet_relative_path="BTCUSDT/other.parquet",
            parquet_sha256="c" * 64,
            row_count=1,
            coverage={},
            gaps=[],
        )


@pytest.mark.asyncio
async def test_snapshot_manifest_failure_preserves_coverage_and_rejects_escape(ledger):
    snapshot_id = f"failed-{uuid4().hex}"
    manifest = CampaignCandleSnapshotManifest(
        snapshot_id=snapshot_id,
        account_id="snapshot-account",
        strategy_id="spike_short",
        campaign_id=f"campaign-{uuid4().hex}",
        symbol="BTCUSDT",
        run_id="run-2",
        signal_time_ms=10_000,
        window_start_ms=9_000,
        window_end_ms=11_000,
        release_hash="d" * 64,
    )
    await ledger.create_campaign_snapshot(manifest)
    failed = await ledger.fail_campaign_snapshot(
        snapshot_id,
        reason="market stream disconnected",
        coverage={"expected_count": 2, "received_count": 1},
        gaps=[{"start_ms": 10_000, "end_ms": 11_000}],
    )
    assert failed.status == "failed"
    assert failed.failure_reason == "market stream disconnected"
    assert failed.gaps == [{"start_ms": 10_000, "end_ms": 11_000}]

    retried_failure = await ledger.fail_campaign_snapshot(
        snapshot_id,
        reason=" market stream disconnected ",
        coverage={"expected_count": 2, "received_count": 1},
        gaps=[{"start_ms": 10_000, "end_ms": 11_000}],
    )
    assert retried_failure.snapshot_id == snapshot_id
    assert retried_failure.failed_at == failed.failed_at

    with pytest.raises(ValueError, match="failed payload conflicts"):
        await ledger.fail_campaign_snapshot(
            snapshot_id,
            reason="different failure",
            coverage={"expected_count": 2, "received_count": 1},
            gaps=[{"start_ms": 10_000, "end_ms": 11_000}],
        )

    with pytest.raises(ValueError, match="relative path"):
        await ledger.complete_campaign_snapshot(
            snapshot_id,
            parquet_relative_path="../escape.parquet",
            parquet_sha256="e" * 64,
            row_count=1,
            coverage={},
            gaps=[],
        )
