"""Runtime adapter for durable per-signal 1s market snapshots.

The live strategy consumes 1s bars continuously, but only a short window around
an entry signal is kept on disk.  This module is deliberately independent from
the execution coordinator: it owns the blocking Parquet writer, the
PostgreSQL manifest lifecycle, and the small local completion outbox used when
PostgreSQL is temporarily unavailable after a Parquet file has been published.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any, Protocol

from trading_platform.ledger.db.models import CampaignCandleSnapshotManifest
from trading_platform.market.live_snapshot import (
    SNAPSHOT_AGGREGATION_VERSION,
    SNAPSHOT_SCHEMA_VERSION,
    LiveSnapshotStore,
    SnapshotManifest,
)
from trading_platform.shared.events import Bar1s


class SnapshotManifestDB(Protocol):
    async def create_campaign_snapshot(
        self, manifest: CampaignCandleSnapshotManifest
    ) -> CampaignCandleSnapshotManifest:
        ...

    async def complete_campaign_snapshot(
        self,
        snapshot_id: str,
        *,
        parquet_relative_path: str,
        parquet_sha256: str,
        row_count: int,
        coverage: dict[str, Any],
        gaps: list[dict[str, Any]],
    ) -> CampaignCandleSnapshotManifest:
        ...

    async def fail_campaign_snapshot(
        self,
        snapshot_id: str,
        *,
        reason: str,
        coverage: dict[str, Any] | None = None,
        gaps: list[dict[str, Any]] | None = None,
    ) -> CampaignCandleSnapshotManifest:
        ...

    async def list_collecting_campaign_snapshots(
        self,
        *,
        account_id: str,
        strategy_id: str,
    ) -> list[CampaignCandleSnapshotManifest]:
        ...

    async def list_failed_campaign_snapshots_missing_event(
        self,
        *,
        account_id: str,
        strategy_id: str,
    ) -> list[CampaignCandleSnapshotManifest]:
        ...


SnapshotEventSink = Callable[..., Awaitable[None]]
ORPHAN_COLLECTING_SNAPSHOT_REASON = (
    "snapshot recovery found collecting manifest without local staging or completion outbox"
)


class SnapshotRuntimeError(RuntimeError):
    """A snapshot could not be made durable before an entry was submitted."""

    def __init__(self, message: str, *, snapshot_id: str | None = None):
        super().__init__(message)
        self.snapshot_id = snapshot_id


class LiveCandleSnapshotRuntime:
    """Async bridge between Spike and the synchronous Parquet snapshot store.

    ``observe_bar`` is called before strategy evaluation.  Signal snapshots are
    created with a PostgreSQL ``collecting`` row before their WAL is started,
    so a signal can never be submitted without a corresponding manifest.
    """

    def __init__(
        self,
        *,
        root: str | Path,
        db: SnapshotManifestDB,
        account_id: str,
        strategy_id: str,
        run_id: str,
        release_hash: str,
        pre_window_ms: int,
        post_window_ms: int,
        event_sink: SnapshotEventSink | None = None,
        store: LiveSnapshotStore | None = None,
    ) -> None:
        normalized_hash = str(release_hash).strip().lower()
        if len(normalized_hash) != 64:
            raise ValueError("snapshot release_hash must be a SHA-256 digest")
        if any(character not in "0123456789abcdef" for character in normalized_hash):
            raise ValueError("snapshot release_hash must be hexadecimal")
        self.db = db
        self.account_id = account_id
        self.strategy_id = strategy_id
        self.run_id = run_id
        self.release_hash = normalized_hash
        self.event_sink = event_sink
        self.store = store or LiveSnapshotStore(
            root,
            pre_window_ms=pre_window_ms,
            post_window_ms=post_window_ms,
        )
        self._snapshot_ids: dict[str, str] = {}
        self._fault_reason: str | None = None
        self._manifest_queue: asyncio.Queue[SnapshotManifest] = asyncio.Queue()
        self._manifest_worker: asyncio.Task[None] | None = None
        self._stopping = False
        self._pending_snapshot_ids: set[str] = set()
        self._store_lock = asyncio.Lock()

    async def _run_store_call(
        self, operation: Callable[..., Any], *args: Any, **kwargs: Any
    ) -> Any:
        """Serialize store access and outlive cancellation of ``to_thread``."""

        async with self._store_lock:
            task = asyncio.create_task(asyncio.to_thread(operation, *args, **kwargs))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError as cancelled:
                # Cancelling the awaiter does not stop its worker thread.  Keep
                # the store lock until that thread has actually returned, even
                # if shutdown cancels this task more than once.
                while not task.done():
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        continue
                    except BaseException:
                        # The worker has finished with an exception.  Leave
                        # it for the result() call below to mark as observed,
                        # then restore the caller's cancellation.
                        break
                # The caller was cancelled while the worker was still
                # running.  Observe the worker outcome so asyncio does not
                # report an unhandled exception, but preserve the caller's
                # cancellation even when the store failed at the same time.
                try:
                    task.result()
                except BaseException:
                    pass
                raise cancelled

    @property
    def failed(self) -> bool:
        return self._fault_reason is not None

    @property
    def fault_reason(self) -> str | None:
        return self._fault_reason

    @property
    def pre_window_ms(self) -> int:
        return self.store.pre_window_ms

    @property
    def post_window_ms(self) -> int:
        return self.store.post_window_ms

    async def start(self) -> None:
        """Replay durable Parquet completion records after a process restart."""
        if self._manifest_worker is not None and not self._manifest_worker.done():
            return
        self._stopping = False
        recovered = await self._run_store_call(
            self.store.recover_staging,
            int(time.time() * 1000),
            finalize_expired=True,
        )
        pending = await self._run_store_call(self.store.pending_manifests)
        self._pending_snapshot_ids = {manifest.snapshot_id for manifest in pending}
        await self._reconcile_collecting_manifests(
            durable_snapshot_ids=set(self.store.active_snapshot_ids)
            | self._pending_snapshot_ids
        )
        self._manifest_worker = asyncio.create_task(
            self._manifest_worker_loop(), name="spike-snapshot-manifest-worker"
        )
        for manifest in pending:
            await self._manifest_queue.put(manifest)
        for manifest in recovered:
            await self._manifest_queue.put(manifest)

    async def _reconcile_collecting_manifests(
        self, *, durable_snapshot_ids: set[str]
    ) -> None:
        """Fail database rows that lost every local recovery record.

        ``ensure_signal_snapshot`` commits PostgreSQL before creating the
        local WAL, so a process crash in that narrow interval can leave a
        collecting row with no local state.  Startup is the first point at
        which both stores can be compared; fail closed there instead of
        leaving an indefinitely collecting orphan visible to the API.
        """

        try:
            newly_failed_ids: set[str] = set()
            collecting = await self.db.list_collecting_campaign_snapshots(
                account_id=self.account_id,
                strategy_id=self.strategy_id,
            )
            for stored in collecting:
                if stored.snapshot_id in durable_snapshot_ids:
                    continue
                await self.db.fail_campaign_snapshot(
                    stored.snapshot_id,
                    reason=ORPHAN_COLLECTING_SNAPSHOT_REASON,
                )
                newly_failed_ids.add(stored.snapshot_id)
                await self._emit_failure(
                    stored.snapshot_id,
                    ORPHAN_COLLECTING_SNAPSHOT_REASON,
                    durable=True,
                )
            # A process can crash after the manifest transaction commits but
            # before the event journal append.  Re-emit every terminal failure
            # at startup; the domain idempotency key makes this safe when the
            # event was already persisted.
            failed = await self.db.list_failed_campaign_snapshots_missing_event(
                account_id=self.account_id,
                strategy_id=self.strategy_id,
            )
            for stored in failed:
                if stored.snapshot_id in newly_failed_ids:
                    continue
                await self._emit_failure(
                    stored.snapshot_id,
                    stored.failure_reason or "snapshot manifest is failed",
                    durable=True,
                )
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            reason = (
                "snapshot startup reconciliation failed: "
                f"{type(exc).__name__}: {exc}"
            )
            self._fault_reason = reason
            raise SnapshotRuntimeError(reason) from exc

    async def observe_bar(self, bar: Bar1s) -> tuple[SnapshotManifest, ...]:
        """Persist a finalized 1s bar into the rolling buffer and active WALs."""

        try:
            manifests = await self._run_store_call(self.store.observe_bar, bar)
            for manifest in manifests:
                await self._manifest_queue.put(manifest)
            return manifests
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            self._fault_reason = (
                f"snapshot bar persistence failed: {type(exc).__name__}: {exc}"
            )
            raise SnapshotRuntimeError(self._fault_reason) from exc

    async def ensure_signal_snapshot(
        self,
        *,
        campaign_id: str,
        symbol: str,
        signal_time_ms: int,
        metadata: Mapping[str, Any] | None = None,
    ) -> str:
        """Create one idempotent collecting snapshot for a signal Campaign."""

        if self._fault_reason is not None:
            raise SnapshotRuntimeError(self._fault_reason)
        existing = self._snapshot_ids.get(campaign_id)
        if existing is not None:
            return existing
        snapshot_id = self.snapshot_id_for_campaign(
            self.account_id, self.strategy_id, campaign_id
        )
        manifest = CampaignCandleSnapshotManifest(
            snapshot_id=snapshot_id,
            account_id=self.account_id,
            strategy_id=self.strategy_id,
            campaign_id=campaign_id,
            symbol=symbol,
            run_id=self.run_id,
            signal_time_ms=int(signal_time_ms),
            window_start_ms=max(0, int(signal_time_ms) - self.pre_window_ms),
            window_end_ms=int(signal_time_ms) + self.post_window_ms,
            schema_version=SNAPSHOT_SCHEMA_VERSION,
            aggregation_version=SNAPSHOT_AGGREGATION_VERSION,
            release_hash=self.release_hash,
        )
        try:
            stored = await self.db.create_campaign_snapshot(manifest)
            status = str(stored.status)
            if status == "failed":
                raise SnapshotRuntimeError(
                    f"snapshot manifest is already failed: {snapshot_id}",
                    snapshot_id=snapshot_id,
                )
            if status == "completed":
                self._snapshot_ids[campaign_id] = snapshot_id
                return snapshot_id
            active_snapshot_ids = await self._run_store_call(
                lambda: self.store.active_snapshot_ids
            )
            if (
                snapshot_id not in active_snapshot_ids
                and snapshot_id not in self._pending_snapshot_ids
            ):
                await self._run_store_call(
                    self.store.start_snapshot,
                    snapshot_id,
                    symbol,
                    int(signal_time_ms),
                    campaign_id=campaign_id,
                    metadata=dict(metadata or {}),
                )
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            reason = f"snapshot start failed: {type(exc).__name__}: {exc}"
            failure_persisted = False
            try:
                await self.db.fail_campaign_snapshot(
                    snapshot_id,
                    reason=reason,
                )
                failure_persisted = True
            except BaseException as fail_exc:
                exc.add_note(
                    "snapshot failure manifest update failed: "
                    f"{type(fail_exc).__name__}: {fail_exc}"
                )
            self._fault_reason = reason
            try:
                await self._emit_failure(
                    snapshot_id,
                    reason,
                    durable=failure_persisted,
                )
            except BaseException as emit_exc:
                exc.add_note(
                    "snapshot failure event emission failed: "
                    f"{type(emit_exc).__name__}: {emit_exc}"
                )
            if isinstance(exc, SnapshotRuntimeError):
                raise
            raise SnapshotRuntimeError(reason, snapshot_id=snapshot_id) from exc
        self._snapshot_ids[campaign_id] = snapshot_id
        await self._emit(
            "market.snapshot_started",
            snapshot_id=snapshot_id,
            campaign_id=campaign_id,
            symbol=symbol,
            event_time=int(signal_time_ms),
            details={
                "window_start_ms": manifest.window_start_ms,
                "window_end_ms": manifest.window_end_ms,
                "release_hash": self.release_hash,
                "schema_version": SNAPSHOT_SCHEMA_VERSION,
                "aggregation_version": SNAPSHOT_AGGREGATION_VERSION,
            },
        )
        return snapshot_id

    async def close(self, *, now_ms: int | None = None) -> None:
        """Seal active windows; incomplete tails remain explicitly marked."""

        try:
            manifests = await self._run_store_call(self.store.close, now_ms)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            self._fault_reason = f"snapshot shutdown failed: {type(exc).__name__}: {exc}"
            await self._emit_failure(None, self._fault_reason)
            return
        for manifest in manifests:
            await self._manifest_queue.put(manifest)
        worker = self._manifest_worker
        if worker is not None:
            try:
                await asyncio.wait_for(self._manifest_queue.join(), timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                if asyncio.current_task() is not None and asyncio.current_task().cancelling():
                    raise
            self._stopping = True
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
            self._manifest_worker = None

    @staticmethod
    def snapshot_id_for_campaign(
        account_id: str, strategy_id: str, campaign_id: str
    ) -> str:
        identity = "\x1f".join((account_id, strategy_id, campaign_id))
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        return f"snapshot-{digest}"

    async def _complete_manifest(self, manifest: SnapshotManifest) -> None:
        await self._manifest_queue.put(manifest)

    async def _manifest_worker_loop(self) -> None:
        while True:
            manifest = await self._manifest_queue.get()
            try:
                try:
                    await self._complete_manifest_now(manifest)
                except asyncio.CancelledError:
                    raise
                except BaseException as exc:
                    reason = (
                        "snapshot manifest delivery failed: "
                        f"{type(exc).__name__}: {exc}"
                    )
                    self._fault_reason = reason
                    try:
                        await self._emit_failure(manifest.snapshot_id, reason)
                    except BaseException:
                        pass
                    if not self._stopping:
                        await asyncio.sleep(5)
                        await self._manifest_queue.put(manifest)
            finally:
                self._manifest_queue.task_done()

    async def _complete_manifest_now(self, manifest: SnapshotManifest) -> None:
        snapshot_id = manifest.snapshot_id
        await self.db.complete_campaign_snapshot(
            snapshot_id,
            parquet_relative_path=manifest.relative_path,
            parquet_sha256=manifest.sha256,
            row_count=manifest.row_count,
            coverage=manifest.coverage,
            gaps=manifest.gaps,
        )
        # The outbox is the durable retry boundary.  Emit the completion fact
        # before acknowledging it so a process crash or journal failure leaves
        # the manifest available for replay after restart.  PostgreSQL
        # completion is idempotent, and the event journal carries the durable
        # completion fact that makes acknowledgement safe.
        await self._emit(
            "market.snapshot_completed",
            snapshot_id=snapshot_id,
            idempotency_key=f"market.snapshot_completed:{snapshot_id}",
            campaign_id=manifest.campaign_id,
            symbol=manifest.symbol,
            event_time=manifest.end_time_ms,
            details={
                "relative_path": manifest.relative_path,
                "sha256": manifest.sha256,
                "row_count": manifest.row_count,
                "coverage": manifest.coverage,
                "gaps": manifest.gaps,
                "complete": manifest.complete,
            },
        )
        await self._run_store_call(self.store.ack_manifest, manifest)
        self._pending_snapshot_ids.discard(manifest.snapshot_id)

    async def _emit_failure(
        self,
        snapshot_id: str | None,
        reason: str,
        *,
        durable: bool = False,
    ) -> None:
        await self._emit(
            "market.snapshot_failed",
            snapshot_id=snapshot_id,
            severity="error",
            idempotency_key=(
                f"market.snapshot_failed:{snapshot_id}"
                if durable and snapshot_id is not None
                else None
            ),
            details={"reason": reason},
        )

    async def _emit(self, event_type: str, **kwargs: Any) -> None:
        if self.event_sink is not None:
            await self.event_sink(event_type, **kwargs)



CandleSnapshotRuntime = LiveCandleSnapshotRuntime

__all__ = [
    "CandleSnapshotRuntime",
    "LiveCandleSnapshotRuntime",
    "ORPHAN_COLLECTING_SNAPSHOT_REASON",
    "SnapshotManifestDB",
    "SnapshotRuntimeError",
]
