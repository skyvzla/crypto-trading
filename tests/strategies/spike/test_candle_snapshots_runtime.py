from __future__ import annotations

import asyncio
import threading
from decimal import Decimal

import pytest

from trading_platform.ledger.db.models import CampaignCandleSnapshotManifest
from trading_platform.shared.events import Bar1s
from trading_platform.strategies.spike.candle_snapshots import (
    LiveCandleSnapshotRuntime,
    ORPHAN_COLLECTING_SNAPSHOT_REASON,
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

    async def list_collecting_campaign_snapshots(self, *, account_id, strategy_id):
        return [
            row
            for row in self.rows.values()
            if row.account_id == account_id
            and row.strategy_id == strategy_id
            and row.status == "collecting"
        ]

    async def list_failed_campaign_snapshots_missing_event(
        self, *, account_id, strategy_id
    ):
        return [
            row
            for row in self.rows.values()
            if row.account_id == account_id
            and row.strategy_id == strategy_id
            and row.status == "failed"
        ]


class BlockingSnapshotStore:
    pre_window_ms = 1_000
    post_window_ms = 2_000

    def __init__(self) -> None:
        self.observe_started = threading.Event()
        self.release_observe = threading.Event()
        self.observe_active = False
        self.closed = False

    def observe_bar(self, _bar):
        self.observe_active = True
        self.observe_started.set()
        self.release_observe.wait()
        self.observe_active = False
        return ()

    def close(self, _now_ms):
        assert not self.observe_active
        self.closed = True
        return ()


class FailingBlockingSnapshotStore(BlockingSnapshotStore):
    def __init__(self) -> None:
        super().__init__()
        self.failure_observed = threading.Event()

    def observe_bar(self, bar):
        super().observe_bar(bar)
        self.failure_observed.set()
        raise RuntimeError("store failed after cancellation")


@pytest.mark.asyncio
async def test_cancelled_observe_waits_for_store_thread_before_close(tmp_path):
    store = BlockingSnapshotStore()
    runtime = LiveCandleSnapshotRuntime(
        root=tmp_path,
        db=MemorySnapshotDB(),
        account_id="acct-a",
        strategy_id="spike_short",
        run_id="run-a",
        release_hash="a" * 64,
        pre_window_ms=1_000,
        post_window_ms=2_000,
        store=store,
    )
    observe_task = asyncio.create_task(runtime.observe_bar(_bar(1_000)))
    assert await asyncio.to_thread(store.observe_started.wait, 1)

    observe_task.cancel()
    await asyncio.sleep(0.02)
    assert not observe_task.done()
    close_task = asyncio.create_task(runtime.close(now_ms=2_000))
    observe_task.cancel()
    await asyncio.sleep(0.02)
    assert not observe_task.done()
    assert not close_task.done()

    store.release_observe.set()
    with pytest.raises(asyncio.CancelledError):
        await observe_task
    await asyncio.wait_for(close_task, 1)
    assert store.closed


@pytest.mark.asyncio
async def test_repeated_cancel_preserves_cancelled_error_when_store_fails(tmp_path):
    store = FailingBlockingSnapshotStore()
    runtime = LiveCandleSnapshotRuntime(
        root=tmp_path,
        db=MemorySnapshotDB(),
        account_id="acct-a",
        strategy_id="spike_short",
        run_id="run-a",
        release_hash="a" * 64,
        pre_window_ms=1_000,
        post_window_ms=2_000,
        store=store,
    )
    observe_task = asyncio.create_task(runtime.observe_bar(_bar(1_000)))
    assert await asyncio.to_thread(store.observe_started.wait, 1)

    observe_task.cancel()
    await asyncio.sleep(0)
    observe_task.cancel()
    store.release_observe.set()
    assert await asyncio.to_thread(store.failure_observed.wait, 1)

    with pytest.raises(asyncio.CancelledError):
        await observe_task
    assert not runtime.failed


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


@pytest.mark.asyncio
async def test_completed_event_failure_keeps_outbox_for_restart_replay(tmp_path):
    db = MemorySnapshotDB()
    first_events: list[str] = []

    async def failing_event_sink(event_type: str, **_kwargs):
        first_events.append(event_type)
        if event_type == "market.snapshot_completed":
            raise RuntimeError("event journal unavailable")

    runtime = LiveCandleSnapshotRuntime(
        root=tmp_path,
        db=db,
        account_id="acct-a",
        strategy_id="spike_short",
        run_id="run-a",
        release_hash="a" * 64,
        pre_window_ms=1_000,
        post_window_ms=2_000,
        event_sink=failing_event_sink,
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
    assert "market.snapshot_completed" in first_events
    pending = await asyncio.to_thread(runtime.store.pending_manifests)
    assert [item.snapshot_id for item in pending] == [snapshot_id]
    await runtime.close(now_ms=5_000)

    replayed_events: list[str] = []

    async def healthy_event_sink(event_type: str, **_kwargs):
        replayed_events.append(event_type)

    restarted = LiveCandleSnapshotRuntime(
        root=tmp_path,
        db=db,
        account_id="acct-a",
        strategy_id="spike_short",
        run_id="run-a-restarted",
        release_hash="a" * 64,
        pre_window_ms=1_000,
        post_window_ms=2_000,
        event_sink=healthy_event_sink,
    )
    await restarted.start()
    await asyncio.sleep(0.05)

    assert "market.snapshot_completed" in replayed_events
    assert await asyncio.to_thread(restarted.store.pending_manifests) == ()
    await restarted.close(now_ms=5_000)


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


@pytest.mark.asyncio
async def test_start_fails_collecting_manifest_without_local_recovery_state(tmp_path):
    db = MemorySnapshotDB()
    snapshot_id = LiveCandleSnapshotRuntime.snapshot_id_for_campaign(
        "acct-a", "spike_short", "spike_short:BTCUSDT:2000"
    )
    await db.create_campaign_snapshot(
        CampaignCandleSnapshotManifest(
            snapshot_id=snapshot_id,
            account_id="acct-a",
            strategy_id="spike_short",
            campaign_id="spike_short:BTCUSDT:2000",
            symbol="BTCUSDT",
            run_id="run-before-crash",
            signal_time_ms=2_000,
            window_start_ms=1_000,
            window_end_ms=4_000,
            release_hash="a" * 64,
        )
    )
    runtime = LiveCandleSnapshotRuntime(
        root=tmp_path,
        db=db,
        account_id="acct-a",
        strategy_id="spike_short",
        run_id="run-after-restart",
        release_hash="a" * 64,
        pre_window_ms=1_000,
        post_window_ms=2_000,
    )

    await runtime.start()

    assert db.rows[snapshot_id].status == "failed"
    assert db.rows[snapshot_id].failure_reason == ORPHAN_COLLECTING_SNAPSHOT_REASON
    await runtime.close(now_ms=5_000)


@pytest.mark.asyncio
async def test_start_replays_failed_manifest_without_a_failure_event(tmp_path):
    db = MemorySnapshotDB()
    snapshot_id = LiveCandleSnapshotRuntime.snapshot_id_for_campaign(
        "acct-a", "spike_short", "spike_short:BTCUSDT:3000"
    )
    await db.create_campaign_snapshot(
        CampaignCandleSnapshotManifest(
            snapshot_id=snapshot_id,
            account_id="acct-a",
            strategy_id="spike_short",
            campaign_id="spike_short:BTCUSDT:3000",
            symbol="BTCUSDT",
            run_id="run-before-crash",
            signal_time_ms=3_000,
            window_start_ms=2_000,
            window_end_ms=4_000,
            release_hash="a" * 64,
        )
    )
    await db.fail_campaign_snapshot(snapshot_id, reason="postgres commit completed")
    events: list[tuple[str, dict[str, object]]] = []

    async def event_sink(event_type: str, **kwargs):
        events.append((event_type, kwargs))

    runtime = LiveCandleSnapshotRuntime(
        root=tmp_path,
        db=db,
        account_id="acct-a",
        strategy_id="spike_short",
        run_id="run-after-crash",
        release_hash="a" * 64,
        pre_window_ms=1_000,
        post_window_ms=1_000,
        event_sink=event_sink,
    )
    await runtime.start()

    assert events == [
        (
            "market.snapshot_failed",
            {
                "snapshot_id": snapshot_id,
                "severity": "error",
                "idempotency_key": f"market.snapshot_failed:{snapshot_id}",
                "details": {"reason": "postgres commit completed"},
            },
        )
    ]
    await runtime.close(now_ms=5_000)
