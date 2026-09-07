"""Durable, short-lived market snapshots for live execution review.

The live market path must not depend on the historical archive catching up
with the exchange.  This module keeps a small in-memory pre-signal buffer and
publishes one immutable Parquet file after a signal's post-signal window has
closed.  PostgreSQL can store :class:`SnapshotManifest` without owning the
high-cardinality candle rows.

Only completed 1s bars are persisted.  The campaign API exposes those 1s
rows; the live chart's 1m+ path remains the existing Binance service path.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Any
from uuid import uuid4

import pyarrow as pa
import pyarrow.parquet as pq

from trading_platform.shared.events import Bar1s


SNAPSHOT_SCHEMA_VERSION = 1
SNAPSHOT_AGGREGATION_VERSION = 1
SNAPSHOT_TIMEFRAME = "1s"
DEFAULT_PRE_WINDOW_MS = 2 * 60 * 1000
DEFAULT_POST_WINDOW_MS = 5 * 60 * 1000

_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_DECIMAL_PRECISION = 38
_DECIMAL_SCALE = 18
_DECIMAL_TYPE = pa.decimal128(_DECIMAL_PRECISION, _DECIMAL_SCALE)

# Keep this list in the same order as the serialised Bar1s payload.  Fields
# are intentionally primitive Arrow types because the historical archive uses
# the same representation and DuckDB can read it without extension types.
SNAPSHOT_COLUMNS = (
    "symbol",
    "timeframe",
    "timestamp",
    "available_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "trade_count",
    "vwap",
    "quote_volume",
    "raw_trade_count",
    "taker_buy_volume",
    "taker_sell_volume",
    "taker_buy_quote_volume",
    "taker_sell_quote_volume",
    "taker_buy_trade_count",
    "taker_sell_trade_count",
    "taker_buy_agg_trade_count",
    "taker_sell_agg_trade_count",
    "max_agg_trade_quantity",
    "max_taker_buy_agg_trade_quantity",
    "max_taker_sell_agg_trade_quantity",
    "first_aggregate_trade_id",
    "last_aggregate_trade_id",
    "first_trade_id",
    "last_trade_id",
)

SNAPSHOT_SCHEMA = pa.schema(
    [
        ("symbol", pa.string()),
        ("timeframe", pa.string()),
        ("timestamp", pa.int64()),
        ("available_time", pa.int64()),
        ("open", _DECIMAL_TYPE),
        ("high", _DECIMAL_TYPE),
        ("low", _DECIMAL_TYPE),
        ("close", _DECIMAL_TYPE),
        ("volume", _DECIMAL_TYPE),
        ("trade_count", pa.int64()),
        ("vwap", _DECIMAL_TYPE),
        ("quote_volume", _DECIMAL_TYPE),
        ("raw_trade_count", pa.int64()),
        ("taker_buy_volume", _DECIMAL_TYPE),
        ("taker_sell_volume", _DECIMAL_TYPE),
        ("taker_buy_quote_volume", _DECIMAL_TYPE),
        ("taker_sell_quote_volume", _DECIMAL_TYPE),
        ("taker_buy_trade_count", pa.int64()),
        ("taker_sell_trade_count", pa.int64()),
        ("taker_buy_agg_trade_count", pa.int64()),
        ("taker_sell_agg_trade_count", pa.int64()),
        ("max_agg_trade_quantity", _DECIMAL_TYPE),
        ("max_taker_buy_agg_trade_quantity", _DECIMAL_TYPE),
        ("max_taker_sell_agg_trade_quantity", _DECIMAL_TYPE),
        ("first_aggregate_trade_id", pa.int64()),
        ("last_aggregate_trade_id", pa.int64()),
        ("first_trade_id", pa.int64()),
        ("last_trade_id", pa.int64()),
    ]
)

def _normalise_symbol(symbol: str) -> str:
    if not isinstance(symbol, str):
        raise ValueError("symbol must be a string")
    value = symbol.strip().upper()
    _validate_component(value, "symbol")
    return value


def _validate_component(value: str, name: str) -> str:
    if not isinstance(value, str) or not value or not _SAFE_COMPONENT.fullmatch(value):
        raise ValueError(f"invalid {name}: path separators and traversal are forbidden")
    if value in {".", ".."}:
        raise ValueError(f"invalid {name}: path traversal is forbidden")
    return value


def _decimal_value(value: Decimal | int | float | None, *, field: str) -> Decimal | None:
    if value is None:
        return None
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"{field} must be a valid decimal") from error
    if not decimal.is_finite():
        raise ValueError(f"{field} must be finite")
    try:
        with localcontext() as context:
            context.prec = 80
            quantized = decimal.quantize(Decimal("1e-18"))
    except InvalidOperation as error:
        raise ValueError(f"{field} exceeds snapshot decimal precision") from error
    if quantized != decimal:
        raise ValueError(
            f"{field} exceeds {_DECIMAL_SCALE} decimal places required by snapshot storage"
        )
    if quantized.copy_abs() >= Decimal(10) ** (_DECIMAL_PRECISION - _DECIMAL_SCALE):
        raise ValueError(f"{field} exceeds snapshot decimal precision")
    return quantized


def _bar_row(bar: Bar1s) -> dict[str, Any]:
    """Convert a Bar1s into the stable on-disk row representation."""

    return {
        "symbol": _normalise_symbol(bar.symbol),
        "timeframe": SNAPSHOT_TIMEFRAME,
        "timestamp": int(bar.timestamp),
        "available_time": int(bar.available_time),
        "open": _decimal_value(bar.open, field="open"),
        "high": _decimal_value(bar.high, field="high"),
        "low": _decimal_value(bar.low, field="low"),
        "close": _decimal_value(bar.close, field="close"),
        "volume": _decimal_value(bar.volume, field="volume"),
        "trade_count": int(bar.trade_count),
        "vwap": _decimal_value(bar.vwap, field="vwap"),
        "quote_volume": _decimal_value(bar.quote_volume, field="quote_volume"),
        "raw_trade_count": bar.raw_trade_count,
        "taker_buy_volume": _decimal_value(bar.taker_buy_volume, field="taker_buy_volume"),
        "taker_sell_volume": _decimal_value(bar.taker_sell_volume, field="taker_sell_volume"),
        "taker_buy_quote_volume": _decimal_value(
            bar.taker_buy_quote_volume, field="taker_buy_quote_volume"
        ),
        "taker_sell_quote_volume": _decimal_value(
            bar.taker_sell_quote_volume, field="taker_sell_quote_volume"
        ),
        "taker_buy_trade_count": bar.taker_buy_trade_count,
        "taker_sell_trade_count": bar.taker_sell_trade_count,
        "taker_buy_agg_trade_count": bar.taker_buy_agg_trade_count,
        "taker_sell_agg_trade_count": bar.taker_sell_agg_trade_count,
        "max_agg_trade_quantity": _decimal_value(
            bar.max_agg_trade_quantity, field="max_agg_trade_quantity"
        ),
        "max_taker_buy_agg_trade_quantity": _decimal_value(
            bar.max_taker_buy_agg_trade_quantity,
            field="max_taker_buy_agg_trade_quantity",
        ),
        "max_taker_sell_agg_trade_quantity": _decimal_value(
            bar.max_taker_sell_agg_trade_quantity,
            field="max_taker_sell_agg_trade_quantity",
        ),
        "first_aggregate_trade_id": bar.first_aggregate_trade_id,
        "last_aggregate_trade_id": bar.last_aggregate_trade_id,
        "first_trade_id": bar.first_trade_id,
        "last_trade_id": bar.last_trade_id,
    }


def _as_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


def _bar_from_dict(data: Mapping[str, Any]) -> Bar1s:
    """Restore a Bar1s from either its JSON/WAL representation."""

    return Bar1s.from_dict(dict(data))


@dataclass(frozen=True)
class SnapshotManifest:
    """Immutable metadata that can be persisted in the execution database."""

    snapshot_id: str
    symbol: str
    path: Path
    sha256: str
    signal_time_ms: int | None
    start_time_ms: int | None
    end_time_ms: int | None
    row_count: int
    complete: bool
    first_aggregate_trade_id: int | None
    last_aggregate_trade_id: int | None
    first_trade_id: int | None
    last_trade_id: int | None
    missing_seconds: tuple[int, ...] = ()
    empty_seconds: tuple[int, ...] = ()
    boundary_missing_seconds: tuple[int, ...] = ()
    agg_trade_gaps: tuple[tuple[int, int], ...] = ()
    agg_trade_non_monotonic: tuple[tuple[int, int], ...] = ()
    pre_window_covered: bool = True
    post_window_covered: bool = True
    schema_version: int = SNAPSHOT_SCHEMA_VERSION
    aggregation_version: int = SNAPSHOT_AGGREGATION_VERSION
    campaign_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    root: Path | None = field(default=None, repr=False, compare=False)

    @property
    def relative_path(self) -> str:
        """Return a portable path suitable for a PostgreSQL manifest."""

        if self.root is None:
            return self.path.name
        try:
            return str(self.path.relative_to(self.root))
        except ValueError:
            return self.path.name

    @property
    def continuity_ok(self) -> bool:
        # A window boundary can be outside the period observed by the feed;
        # that is coverage information, not evidence of an interrupted feed.
        return not (
            self.missing_seconds
            or self.agg_trade_gaps
            or self.agg_trade_non_monotonic
        )

    @property
    def coverage_status(self) -> str:
        if (
            self.missing_seconds
            or self.agg_trade_gaps
            or self.agg_trade_non_monotonic
        ):
            return "gapped"
        if (
            not self.complete
            or self.boundary_missing_seconds
            or not self.pre_window_covered
            or not self.post_window_covered
        ):
            return "incomplete"
        return "complete"

    @property
    def expected_row_count(self) -> int | None:
        if self.start_time_ms is None or self.end_time_ms is None:
            return None
        start = _ceil_second(self.start_time_ms)
        end = _ceil_second(self.end_time_ms)
        return max(0, (end - start) // 1_000)

    @property
    def coverage(self) -> dict[str, Any]:
        expected = self.expected_row_count
        received = self.row_count + len(self.empty_seconds)
        return {
            "expected_rows": expected,
            "present_rows": self.row_count,
            "empty_rows": len(self.empty_seconds),
            "missing_rows": len(self.missing_seconds),
            "boundary_missing_rows": len(self.boundary_missing_seconds),
            "coverage_expected": expected,
            "coverage_received": received if expected is not None else None,
            "start_time_ms": self.start_time_ms,
            "end_time_ms": self.end_time_ms,
            "complete": self.complete,
            "continuity_ok": self.continuity_ok,
            "coverage_status": self.coverage_status,
            "pre_window_covered": self.pre_window_covered,
            "post_window_covered": self.post_window_covered,
        }

    @property
    def gaps(self) -> list[dict[str, Any]]:
        return [
            {"type": "missing_second", "timestamp_ms": timestamp}
            for timestamp in self.missing_seconds
        ] + [
            {
                "type": "aggregate_trade_gap",
                "start_id": start_id,
                "end_id": end_id,
            }
            for start_id, end_id in self.agg_trade_gaps
        ] + [
            {
                "type": "aggregate_trade_non_monotonic",
                "expected_first_id": expected_first_id,
                "actual_first_id": actual_first_id,
            }
            for expected_first_id, actual_first_id in self.agg_trade_non_monotonic
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "symbol": self.symbol,
            "path": str(self.path),
            "relative_path": self.relative_path,
            "sha256": self.sha256,
            "signal_time_ms": self.signal_time_ms,
            "start_time_ms": self.start_time_ms,
            "end_time_ms": self.end_time_ms,
            "row_count": self.row_count,
            "complete": self.complete,
            "continuity_ok": self.continuity_ok,
            "coverage": self.coverage,
            "gaps": self.gaps,
            "first_aggregate_trade_id": self.first_aggregate_trade_id,
            "last_aggregate_trade_id": self.last_aggregate_trade_id,
            "first_trade_id": self.first_trade_id,
            "last_trade_id": self.last_trade_id,
            "missing_seconds": list(self.missing_seconds),
            "empty_seconds": list(self.empty_seconds),
            "boundary_missing_seconds": list(self.boundary_missing_seconds),
            "agg_trade_gaps": [list(item) for item in self.agg_trade_gaps],
            "agg_trade_non_monotonic": [
                list(item) for item in self.agg_trade_non_monotonic
            ],
            "pre_window_covered": self.pre_window_covered,
            "post_window_covered": self.post_window_covered,
            "schema_version": self.schema_version,
            "aggregation_version": self.aggregation_version,
            "campaign_id": self.campaign_id,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(
        cls, data: Mapping[str, Any], *, root: Path | None = None
    ) -> "SnapshotManifest":
        relative_path = data.get("relative_path")
        raw_path = data.get("path")
        if relative_path is not None and root is not None:
            path = (root / str(relative_path)).resolve()
            try:
                path.relative_to(root.resolve())
            except ValueError as error:
                raise ValueError("snapshot manifest path escapes root") from error
        elif raw_path is not None:
            path = Path(str(raw_path)).resolve()
        else:
            raise ValueError("snapshot manifest has no path")
        gaps = data.get("agg_trade_gaps") or []
        non_monotonic = data.get("agg_trade_non_monotonic") or []
        return cls(
            snapshot_id=_validate_component(str(data["snapshot_id"]), "snapshot_id"),
            symbol=_normalise_symbol(str(data["symbol"])),
            path=path,
            sha256=str(data["sha256"]).lower(),
            signal_time_ms=data.get("signal_time_ms"),
            start_time_ms=data.get("start_time_ms"),
            end_time_ms=data.get("end_time_ms"),
            row_count=int(data.get("row_count", 0)),
            complete=bool(data.get("complete", False)),
            first_aggregate_trade_id=data.get("first_aggregate_trade_id"),
            last_aggregate_trade_id=data.get("last_aggregate_trade_id"),
            first_trade_id=data.get("first_trade_id"),
            last_trade_id=data.get("last_trade_id"),
            missing_seconds=tuple(int(value) for value in data.get("missing_seconds", ())),
            empty_seconds=tuple(int(value) for value in data.get("empty_seconds", ())),
            boundary_missing_seconds=tuple(
                int(value) for value in data.get("boundary_missing_seconds", ())
            ),
            agg_trade_gaps=tuple(
                (int(item[0]), int(item[1])) for item in gaps
            ),
            agg_trade_non_monotonic=tuple(
                (int(item[0]), int(item[1])) for item in non_monotonic
            ),
            pre_window_covered=bool(data.get("pre_window_covered", True)),
            post_window_covered=bool(data.get("post_window_covered", True)),
            schema_version=int(data.get("schema_version", SNAPSHOT_SCHEMA_VERSION)),
            aggregation_version=int(
                data.get("aggregation_version", SNAPSHOT_AGGREGATION_VERSION)
            ),
            campaign_id=data.get("campaign_id"),
            metadata=data.get("metadata") or {},
            root=root,
        )


@dataclass
class SnapshotCapture:
    """An active signal window held by :class:`LiveSnapshotStore`."""

    snapshot_id: str
    symbol: str
    signal_time_ms: int
    start_time_ms: int
    due_time_ms: int
    campaign_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    pre_window_covered: bool = False
    post_window_covered: bool = False
    bars: dict[int, Bar1s] = field(default_factory=dict, repr=False)
    finalizing_complete: bool | None = None
    finalizing_end_time_ms: int | None = None
    finalizing_post_window_covered: bool | None = None

    def add_bar(self, bar: Bar1s) -> None:
        timestamp = int(bar.timestamp)
        previous = self.bars.get(timestamp)
        if previous is None:
            self.bars[timestamp] = bar
            return
        # Replayed delivery of the exact same bar is harmless.  A different
        # bar for one timestamp is a data-quality error and must be visible.
        if _bar_row(previous) != _bar_row(bar):
            raise ValueError(
                f"snapshot {self.snapshot_id} received conflicting bar at {timestamp}"
            )


class LiveSnapshotStore:
    """Synchronous live 1s snapshot manager and Parquet reader.

    ``on_manifest`` runs synchronously after the file has been fsynced and
    atomically renamed.  It should only persist the manifest or enqueue a
    small follow-up job; it must not be required for the Parquet file's
    durability.
    """

    def __init__(
        self,
        root: str | Path,
        *,
        pre_window_ms: int = DEFAULT_PRE_WINDOW_MS,
        post_window_ms: int = DEFAULT_POST_WINDOW_MS,
        max_buffer_ms: int | None = None,
        on_manifest: Callable[[SnapshotManifest], None] | None = None,
    ) -> None:
        if pre_window_ms < 0 or post_window_ms < 0:
            raise ValueError("snapshot windows must be non-negative")
        if max_buffer_ms is not None and max_buffer_ms < pre_window_ms:
            raise ValueError("max_buffer_ms must cover pre_window_ms")
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.pre_window_ms = int(pre_window_ms)
        self.post_window_ms = int(post_window_ms)
        self.max_buffer_ms = int(
            pre_window_ms if max_buffer_ms is None else max_buffer_ms
        )
        self.on_manifest = on_manifest
        self._buffers: dict[str, deque[Bar1s]] = {}
        self._active: dict[str, SnapshotCapture] = {}
        self._last_observed_time_ms = 0
        self._staging_root = self.root / ".staging"
        self._staging_root.mkdir(parents=True, exist_ok=True)
        self._outbox_root = self.root / ".outbox"
        self._outbox_root.mkdir(parents=True, exist_ok=True)
        pending_ids = self._recover_outbox_ids()
        self._recover_staging(skip_snapshot_ids=pending_ids)

    @property
    def active_snapshot_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._active))

    def path_for_snapshot(self, snapshot_id: str, symbol: str) -> Path:
        sid = _validate_component(snapshot_id, "snapshot_id")
        normalised_symbol = _normalise_symbol(symbol)
        path = (self.root / normalised_symbol / f"{sid}.parquet").resolve()
        self._assert_inside_root(path)
        return path

    def _assert_inside_root(self, path: Path) -> None:
        try:
            path.relative_to(self.root)
        except ValueError as error:
            raise ValueError("snapshot path escapes configured root") from error

    def _append_buffer(self, bar: Bar1s) -> None:
        symbol = _normalise_symbol(bar.symbol)
        buffer = self._buffers.setdefault(symbol, deque())
        buffer.append(bar)
        newest = int(bar.timestamp)
        cutoff = newest - self.max_buffer_ms
        if buffer:
            self._buffers[symbol] = deque(
                item
                for item in buffer
                if int(item.timestamp) >= cutoff
            )

    def observe_bar(
        self, bar: Bar1s, *, now_ms: int | None = None
    ) -> tuple[SnapshotManifest, ...]:
        """Observe a finalized 1s bar and publish windows that became due."""

        if not isinstance(bar, Bar1s):
            raise TypeError("observe_bar expects Bar1s")
        if int(bar.timestamp) % 1_000:
            raise ValueError("Bar1s timestamp must be aligned to one second")
        symbol = _normalise_symbol(bar.symbol)
        self._append_buffer(bar)
        self._last_observed_time_ms = max(
            self._last_observed_time_ms,
            int(now_ms) if now_ms is not None else int(bar.available_time),
        )
        for capture in self._active.values():
            if capture.symbol != symbol:
                continue
            if capture.finalizing_complete is not None:
                continue
            if capture.start_time_ms <= bar.timestamp < capture.due_time_ms:
                was_present = int(bar.timestamp) in capture.bars
                capture.add_bar(bar)
                if not was_present:
                    self._append_wal_bar(capture, bar)
        return self.flush_due(self._last_observed_time_ms)

    # ``append`` is a concise alias for callers that already have a bar stream.
    append = observe_bar

    def start_snapshot(
        self,
        snapshot_id: str | Mapping[str, Any] | Any | None = None,
        symbol: str | None = None,
        signal_time_ms: int | None = None,
        *,
        campaign_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> SnapshotCapture:
        """Start a capture, copying the currently buffered pre-signal bars.

        For coordinator convenience the first argument may also be a mapping
        or an object with ``symbol`` and ``event_time``/``signal_time_ms``
        attributes (for example ``StrategyAuditEvent``).  The explicit form is
        preferred when a campaign supplies its own stable snapshot ID.
        """

        signal_metadata: dict[str, Any] = {}
        if not isinstance(snapshot_id, str) and snapshot_id is not None:
            signal = snapshot_id
            snapshot_id = None
            if isinstance(signal, Mapping):
                symbol = symbol or signal.get("symbol")
                signal_time_ms = signal_time_ms or signal.get("signal_time_ms")
                signal_time_ms = signal_time_ms or signal.get("event_time")
                campaign_id = campaign_id or signal.get("campaign_id")
                snapshot_id = signal.get("snapshot_id") or signal.get("id")
                details = signal.get("details") or signal.get("metadata")
                if isinstance(details, Mapping):
                    signal_metadata.update(details)
            else:
                symbol = symbol or getattr(signal, "symbol", None)
                signal_time_ms = signal_time_ms or getattr(signal, "signal_time_ms", None)
                signal_time_ms = signal_time_ms or getattr(signal, "event_time", None)
                campaign_id = campaign_id or getattr(signal, "campaign_id", None)
                snapshot_id = getattr(signal, "snapshot_id", None)
                details = getattr(signal, "details", None)
                if isinstance(details, Mapping):
                    signal_metadata.update(details)
        if symbol is None or signal_time_ms is None:
            raise ValueError("symbol and signal_time_ms are required")
        normalised_symbol = _normalise_symbol(symbol)
        signal_time = int(signal_time_ms)
        if signal_time < 0:
            raise ValueError("signal_time_ms must be non-negative")
        sid = str(snapshot_id or f"snapshot-{uuid4().hex}")
        _validate_component(sid, "snapshot_id")
        if sid in self._active:
            raise ValueError(f"snapshot already active: {sid}")
        merged_metadata = dict(signal_metadata)
        if metadata:
            merged_metadata.update(metadata)
        start_time = max(0, signal_time - self.pre_window_ms)
        due_time = signal_time + self.post_window_ms
        buffer = self._buffers.get(normalised_symbol, ())
        pre_window_covered = bool(buffer) and int(buffer[0].timestamp) <= start_time
        capture = SnapshotCapture(
            snapshot_id=sid,
            symbol=normalised_symbol,
            signal_time_ms=signal_time,
            start_time_ms=start_time,
            due_time_ms=due_time,
            campaign_id=campaign_id,
            metadata=merged_metadata,
            pre_window_covered=pre_window_covered,
        )
        for bar in buffer:
            if start_time <= bar.timestamp <= signal_time:
                capture.add_bar(bar)
        self._active[sid] = capture
        try:
            self._write_wal(capture)
        except BaseException:
            self._active.pop(sid, None)
            raise
        return capture

    def recover_staging(
        self, now_ms: int | None = None, *, finalize_expired: bool = False
    ) -> tuple[SnapshotManifest, ...]:
        """Recover active JSONL windows and optionally seal expired ones.

        Construction always restores staging files.  Callers that have a
        trusted wall-clock value after startup may pass it here; setting
        ``finalize_expired`` seals windows that already passed their due time
        as incomplete, preserving the fact that their post-signal tail was
        interrupted rather than pretending it was captured.
        """

        if now_ms is None:
            return ()
        now = int(now_ms)
        self._last_observed_time_ms = max(self._last_observed_time_ms, now)
        if not finalize_expired:
            return ()
        manifests = []
        for snapshot_id in sorted(tuple(self._active)):
            capture = self._active[snapshot_id]
            if (
                capture.finalizing_complete is not None
                or capture.due_time_ms <= now
            ):
                manifests.append(
                    self._finalize(
                        snapshot_id,
                        complete=False,
                        end_time_ms=capture.due_time_ms,
                    )
                )
        return tuple(manifests)

    def flush_due(self, now_ms: int | None = None) -> tuple[SnapshotManifest, ...]:
        """Publish every active capture whose post-signal window is complete."""

        now = self._last_observed_time_ms if now_ms is None else int(now_ms)
        if now < 0:
            raise ValueError("now_ms must be non-negative")
        self._last_observed_time_ms = max(self._last_observed_time_ms, now)
        due = [
            snapshot_id
            for snapshot_id, capture in self._active.items()
            if capture.due_time_ms <= now
        ]
        manifests = [
            self._finalize(
                snapshot_id,
                complete=True,
                post_window_covered=True,
            )
            for snapshot_id in sorted(due)
        ]
        return tuple(manifests)

    def finalize_snapshot(
        self, snapshot_id: str, end_time_ms: int | None = None
    ) -> SnapshotManifest:
        """Close one capture, optionally marking an early/incomplete end."""

        if snapshot_id not in self._active:
            raise KeyError(f"unknown active snapshot: {snapshot_id}")
        capture = self._active[snapshot_id]
        end_time = capture.due_time_ms if end_time_ms is None else int(end_time_ms)
        end_time = max(capture.start_time_ms, end_time)
        complete = end_time >= capture.due_time_ms
        return self._finalize(snapshot_id, complete=complete, end_time_ms=end_time)

    def close(self, now_ms: int | None = None) -> tuple[SnapshotManifest, ...]:
        """Close all active captures and return their manifests.

        Captures closed before their configured due time remain durable but are
        marked ``complete=False`` so a reviewer can distinguish process
        shutdown from a full post-signal window.
        """

        now = self._last_observed_time_ms if now_ms is None else int(now_ms)
        self._last_observed_time_ms = max(self._last_observed_time_ms, now)
        manifests = []
        for snapshot_id in sorted(tuple(self._active)):
            capture = self._active[snapshot_id]
            manifests.append(
                self._finalize(
                    snapshot_id,
                    complete=now >= capture.due_time_ms,
                    end_time_ms=max(
                        capture.start_time_ms,
                        min(now, capture.due_time_ms),
                    ),
                )
            )
        return tuple(manifests)

    def write_snapshot(
        self,
        snapshot_id: str,
        bars: Sequence[Bar1s],
        *,
        symbol: str | None = None,
        signal_time_ms: int | None = None,
        start_time_ms: int | None = None,
        end_time_ms: int | None = None,
        complete: bool = True,
        campaign_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        pre_window_covered: bool | None = None,
        post_window_covered: bool | None = None,
    ) -> SnapshotManifest:
        """Atomically publish one standalone snapshot file."""

        sid = _validate_component(snapshot_id, "snapshot_id")
        rows = self._validated_rows(bars, symbol=symbol)
        resolved_symbol = _normalise_symbol(symbol or (bars[0].symbol if bars else ""))
        path = self.path_for_snapshot(sid, resolved_symbol)
        if path.exists():
            raise FileExistsError(f"snapshot already exists: {path}")
        return self._publish(
            snapshot_id=sid,
            symbol=resolved_symbol,
            bars=rows,
            path=path,
            signal_time_ms=signal_time_ms,
            start_time_ms=start_time_ms,
            end_time_ms=end_time_ms,
            complete=complete,
            campaign_id=campaign_id,
            metadata=metadata or {},
            pre_window_covered=pre_window_covered,
            post_window_covered=post_window_covered,
        )

    def read_snapshot(
        self,
        snapshot: SnapshotManifest | str | Path,
        *,
        interval: str = SNAPSHOT_TIMEFRAME,
        start_ms: int | None = None,
        end_ms: int | None = None,
    ) -> list[dict[str, Any]]:
        """Read persisted 1s rows from one immutable snapshot."""

        if interval != SNAPSHOT_TIMEFRAME:
            raise ValueError("campaign snapshots only support interval='1s'")

        path = self._resolve_read_path(snapshot)
        table = pq.read_table(path)
        rows = table.to_pylist()
        if start_ms is not None:
            rows = [row for row in rows if int(row["timestamp"]) >= int(start_ms)]
        if end_ms is not None:
            rows = [row for row in rows if int(row["timestamp"]) < int(end_ms)]
        return rows

    def pending_manifests(self) -> tuple[SnapshotManifest, ...]:
        """Return Parquet manifests not yet acknowledged by PostgreSQL.

        The outbox is intentionally independent from ``on_manifest``.  A
        process may publish the Parquet file and crash before its database
        transaction commits; this method lets the runtime retry that delivery
        after restart.
        """

        manifests: list[SnapshotManifest] = []
        for path in sorted(self._outbox_root.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                manifest = SnapshotManifest.from_dict(data, root=self.root)
            except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
                raise RuntimeError(f"invalid snapshot manifest outbox: {path}") from error
            manifests.append(manifest)
        return tuple(manifests)

    def ack_manifest(
        self,
        manifest: SnapshotManifest | str,
        *,
        sha256: str | None = None,
    ) -> None:
        """Acknowledge one manifest after its PostgreSQL write commits."""

        snapshot_id = manifest.snapshot_id if isinstance(manifest, SnapshotManifest) else str(manifest)
        _validate_component(snapshot_id, "snapshot_id")
        path = self._outbox_path(snapshot_id)
        if not path.is_file():
            return
        stored = SnapshotManifest.from_dict(
            json.loads(path.read_text(encoding="utf-8")),
            root=self.root,
        )
        expected_hash = (
            sha256
            if sha256 is not None
            else manifest.sha256
            if isinstance(manifest, SnapshotManifest)
            else None
        )
        if expected_hash is not None and stored.sha256 != str(expected_hash).lower():
            raise ValueError(f"snapshot manifest hash mismatch: {snapshot_id}")
        if not stored.path.is_file():
            raise FileNotFoundError(stored.path)
        if _sha256(stored.path) != stored.sha256:
            raise ValueError(f"snapshot payload hash mismatch: {snapshot_id}")
        # Remove an interrupted capture WAL before acknowledging the sidecar.
        # If the process stops after this point, the sidecar is still pending
        # and can be retried without restoring a capture for an immutable file.
        self._remove_wal(snapshot_id)
        path.unlink()
        self._fsync_directory(path.parent)

    def read_bars(
        self,
        snapshot: SnapshotManifest | str | Path,
        *,
        start_ms: int | None = None,
        end_ms: int | None = None,
    ) -> list[Bar1s]:
        """Read persisted 1s rows back into the shared Bar1s event type."""

        rows = self.read_snapshot(snapshot, interval=SNAPSHOT_TIMEFRAME, start_ms=start_ms, end_ms=end_ms)
        return [
            Bar1s(
                symbol=str(row["symbol"]),
                timestamp=int(row["timestamp"]),
                available_time=int(row["available_time"]),
                open=Decimal(str(row["open"])),
                high=Decimal(str(row["high"])),
                low=Decimal(str(row["low"])),
                close=Decimal(str(row["close"])),
                volume=Decimal(str(row["volume"])),
                trade_count=int(row["trade_count"]),
                vwap=Decimal(str(row["vwap"])),
                quote_volume=_as_decimal(row.get("quote_volume")),
                raw_trade_count=row.get("raw_trade_count"),
                taker_buy_volume=_as_decimal(row.get("taker_buy_volume")),
                taker_sell_volume=_as_decimal(row.get("taker_sell_volume")),
                taker_buy_quote_volume=_as_decimal(row.get("taker_buy_quote_volume")),
                taker_sell_quote_volume=_as_decimal(row.get("taker_sell_quote_volume")),
                taker_buy_trade_count=row.get("taker_buy_trade_count"),
                taker_sell_trade_count=row.get("taker_sell_trade_count"),
                taker_buy_agg_trade_count=row.get("taker_buy_agg_trade_count"),
                taker_sell_agg_trade_count=row.get("taker_sell_agg_trade_count"),
                max_agg_trade_quantity=_as_decimal(row.get("max_agg_trade_quantity")),
                max_taker_buy_agg_trade_quantity=_as_decimal(
                    row.get("max_taker_buy_agg_trade_quantity")
                ),
                max_taker_sell_agg_trade_quantity=_as_decimal(
                    row.get("max_taker_sell_agg_trade_quantity")
                ),
                first_aggregate_trade_id=row.get("first_aggregate_trade_id"),
                last_aggregate_trade_id=row.get("last_aggregate_trade_id"),
                first_trade_id=row.get("first_trade_id"),
                last_trade_id=row.get("last_trade_id"),
            )
            for row in rows
        ]

    def _resolve_read_path(self, snapshot: SnapshotManifest | str | Path) -> Path:
        if isinstance(snapshot, SnapshotManifest):
            path = snapshot.path
        else:
            candidate = Path(snapshot)
            if not candidate.is_absolute():
                if candidate.suffix != ".parquet":
                    candidate = Path(f"{candidate}.parquet")
                # A bare snapshot ID is resolved under each symbol only when
                # the caller supplies a manifest; avoid ambiguous filesystem
                # searches here and use root/<id>.parquet only for convenience.
                path = self.root / candidate
            else:
                path = candidate
        path = path.resolve()
        self._assert_inside_root(path)
        if path.suffix != ".parquet" or not path.is_file():
            raise FileNotFoundError(path)
        return path

    def _validated_rows(
        self, bars: Sequence[Bar1s], *, symbol: str | None = None
    ) -> list[Bar1s]:
        rows = sorted(bars, key=lambda bar: int(bar.timestamp))
        expected_symbol = _normalise_symbol(symbol) if symbol else None
        seen: set[int] = set()
        for bar in rows:
            if not isinstance(bar, Bar1s):
                raise TypeError("snapshot rows must be Bar1s")
            bar_symbol = _normalise_symbol(bar.symbol)
            if expected_symbol is None:
                expected_symbol = bar_symbol
            if bar_symbol != expected_symbol:
                raise ValueError("one snapshot must contain one symbol")
            timestamp = int(bar.timestamp)
            if timestamp % 1_000:
                raise ValueError("Bar1s timestamp must be aligned to one second")
            if timestamp in seen:
                raise ValueError(f"snapshot contains duplicate timestamp: {timestamp}")
            seen.add(timestamp)
        if not rows and symbol is None:
            raise ValueError("symbol is required when writing an empty snapshot")
        return rows

    def _finalize(
        self,
        snapshot_id: str,
        *,
        complete: bool,
        end_time_ms: int | None = None,
        post_window_covered: bool | None = None,
    ) -> SnapshotManifest:
        capture = self._active[snapshot_id]
        bars = self._validated_rows(list(capture.bars.values()), symbol=capture.symbol)
        path = self.path_for_snapshot(capture.snapshot_id, capture.symbol)
        end_time = capture.due_time_ms if end_time_ms is None else int(end_time_ms)
        end_time = max(capture.start_time_ms, end_time)
        resolved_post_window_covered = (
            capture.post_window_covered
            if post_window_covered is None
            else post_window_covered
        )
        if capture.finalizing_complete is None:
            self._append_wal_finalize(
                capture,
                complete=complete,
                end_time_ms=end_time,
                post_window_covered=resolved_post_window_covered,
            )
            capture.finalizing_complete = bool(complete)
            capture.finalizing_end_time_ms = end_time
            capture.finalizing_post_window_covered = bool(
                resolved_post_window_covered
            )
        else:
            complete = capture.finalizing_complete
            assert capture.finalizing_end_time_ms is not None
            assert capture.finalizing_post_window_covered is not None
            end_time = capture.finalizing_end_time_ms
            resolved_post_window_covered = (
                capture.finalizing_post_window_covered
            )
        manifest = self._publish(
            snapshot_id=capture.snapshot_id,
            symbol=capture.symbol,
            bars=bars,
            path=path,
            signal_time_ms=capture.signal_time_ms,
            start_time_ms=capture.start_time_ms,
            end_time_ms=end_time,
            complete=complete,
            campaign_id=capture.campaign_id,
            metadata=capture.metadata,
            pre_window_covered=capture.pre_window_covered,
            post_window_covered=resolved_post_window_covered,
            recover_existing=True,
        )
        self._remove_wal(snapshot_id)
        self._active.pop(snapshot_id, None)
        return manifest

    def _publish(
        self,
        *,
        snapshot_id: str,
        symbol: str,
        bars: Sequence[Bar1s],
        path: Path,
        signal_time_ms: int | None,
        start_time_ms: int | None,
        end_time_ms: int | None,
        complete: bool,
        campaign_id: str | None,
        metadata: Mapping[str, Any],
        pre_window_covered: bool | None = None,
        post_window_covered: bool | None = None,
        recover_existing: bool = False,
    ) -> SnapshotManifest:
        if (
            start_time_ms is not None
            and end_time_ms is not None
            and int(end_time_ms) < int(start_time_ms)
        ):
            raise ValueError("snapshot end_time_ms cannot precede start_time_ms")
        rows = [_bar_row(bar) for bar in bars]
        table = pa.Table.from_pydict(
            {
                column: pa.array(
                    [row.get(column) for row in rows],
                    type=SNAPSHOT_SCHEMA.field(column).type,
                )
                for column in SNAPSHOT_COLUMNS
            },
            schema=SNAPSHOT_SCHEMA,
        )
        parquet_metadata = {
            b"trading_platform.snapshot_schema_version": str(SNAPSHOT_SCHEMA_VERSION).encode(),
            b"trading_platform.snapshot_aggregation_version": str(
                SNAPSHOT_AGGREGATION_VERSION
            ).encode(),
            b"trading_platform.snapshot_id": snapshot_id.encode(),
            b"trading_platform.symbol": symbol.encode(),
        }
        table = table.replace_schema_metadata(parquet_metadata)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._assert_inside_root(path)
        if path.exists():
            if not recover_existing:
                raise FileExistsError(f"snapshot already exists: {path}")
            self._validate_existing_payload(path, table)
        else:
            temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
            try:
                pq.write_table(table, temporary, compression="zstd")
                self._fsync_file(temporary)
                os.replace(temporary, path)
                self._fsync_directory(path.parent)
            except BaseException:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
                raise

        (
            empty_seconds,
            missing_seconds,
            boundary_missing_seconds,
            agg_trade_gaps,
            agg_trade_non_monotonic,
        ) = _continuity(
            rows,
            start_time_ms=start_time_ms,
            end_time_ms=end_time_ms,
        )
        if pre_window_covered is None:
            pre_window_covered = _covers_start(rows, start_time_ms)
        if post_window_covered is None:
            post_window_covered = _covers_end(rows, end_time_ms)
        first_agg, last_agg = _id_bounds(rows, "first_aggregate_trade_id")
        first_trade, last_trade = _id_bounds(rows, "first_trade_id")
        digest = _sha256(path)
        manifest = SnapshotManifest(
            snapshot_id=snapshot_id,
            symbol=symbol,
            path=path,
            sha256=digest,
            signal_time_ms=signal_time_ms,
            start_time_ms=start_time_ms,
            end_time_ms=end_time_ms,
            row_count=len(rows),
            complete=bool(complete),
            first_aggregate_trade_id=first_agg,
            last_aggregate_trade_id=last_agg,
            first_trade_id=first_trade,
            last_trade_id=last_trade,
            missing_seconds=missing_seconds,
            empty_seconds=empty_seconds,
            boundary_missing_seconds=boundary_missing_seconds,
            agg_trade_gaps=agg_trade_gaps,
            agg_trade_non_monotonic=agg_trade_non_monotonic,
            pre_window_covered=bool(pre_window_covered),
            post_window_covered=bool(post_window_covered),
            aggregation_version=SNAPSHOT_AGGREGATION_VERSION,
            campaign_id=campaign_id,
            metadata=dict(metadata),
            root=self.root,
        )
        self._write_outbox(manifest)
        if self.on_manifest is not None:
            self.on_manifest(manifest)
        return manifest

    @staticmethod
    def _validate_existing_payload(path: Path, expected: pa.Table) -> None:
        """Accept only the exact payload published by an interrupted finalize."""

        try:
            published = pq.read_table(path)
        except BaseException as error:
            raise ValueError(
                f"existing snapshot payload is unreadable: {path}"
            ) from error
        if not published.equals(expected, check_metadata=True):
            raise ValueError(
                f"existing snapshot payload conflicts with recovered WAL: {path}"
            )

    def _wal_path(self, snapshot_id: str) -> Path:
        path = (self._staging_root / f"{_validate_component(snapshot_id, 'snapshot_id')}.jsonl").resolve()
        self._assert_inside_root(path)
        return path

    def _outbox_path(self, snapshot_id: str) -> Path:
        path = (
            self._outbox_root
            / f"{_validate_component(snapshot_id, 'snapshot_id')}.json"
        ).resolve()
        self._assert_inside_root(path)
        return path

    def _recover_outbox_ids(self) -> set[str]:
        """Return payload IDs whose durable manifest still needs delivery."""

        pending: set[str] = set()
        for path in self._outbox_root.glob("*.json"):
            try:
                pending.add(_validate_component(path.stem, "snapshot_id"))
            except ValueError:
                # Keep malformed sidecars available for operator inspection;
                # they must not prevent unrelated captures from recovering.
                continue
        return pending

    def _write_outbox(self, manifest: SnapshotManifest) -> None:
        """Durably stage a manifest before notifying the database worker."""

        path = self._outbox_path(manifest.snapshot_id)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        payload = json.dumps(
            manifest.to_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
        try:
            with temporary.open("w", encoding="utf-8") as target:
                target.write(payload)
                target.write("\n")
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, path)
            self._fsync_directory(path.parent)
        except BaseException:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            raise

    def _write_wal(self, capture: SnapshotCapture) -> None:
        """Write the complete initial capture before acknowledging start."""

        path = self._wal_path(capture.snapshot_id)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        record = {
            "type": "start",
            "snapshot_id": capture.snapshot_id,
            "symbol": capture.symbol,
            "signal_time_ms": capture.signal_time_ms,
            "start_time_ms": capture.start_time_ms,
            "due_time_ms": capture.due_time_ms,
            "campaign_id": capture.campaign_id,
            "metadata": dict(capture.metadata),
            "pre_window_covered": capture.pre_window_covered,
            "post_window_covered": capture.post_window_covered,
        }
        try:
            with temporary.open("w", encoding="utf-8") as target:
                target.write(json.dumps(record, sort_keys=True, separators=(",", ":")))
                target.write("\n")
                for bar in sorted(capture.bars.values(), key=lambda item: item.timestamp):
                    target.write(
                        json.dumps(
                            {"type": "bar", "bar": bar.to_dict()},
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                    )
                    target.write("\n")
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, path)
            self._fsync_directory(path.parent)
        except BaseException:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            raise

    def _append_wal_bar(self, capture: SnapshotCapture, bar: Bar1s) -> None:
        path = self._wal_path(capture.snapshot_id)
        with path.open("a", encoding="utf-8") as target:
            target.write(
                json.dumps(
                    {"type": "bar", "bar": bar.to_dict()},
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            target.write("\n")
            target.flush()
            os.fsync(target.fileno())

    def _append_wal_finalize(
        self,
        capture: SnapshotCapture,
        *,
        complete: bool,
        end_time_ms: int,
        post_window_covered: bool,
    ) -> None:
        """Persist the immutable finalization decision before publishing."""

        path = self._wal_path(capture.snapshot_id)
        record = {
            "type": "finalize",
            "complete": bool(complete),
            "end_time_ms": int(end_time_ms),
            "post_window_covered": bool(post_window_covered),
        }
        with path.open("a", encoding="utf-8") as target:
            target.write(json.dumps(record, sort_keys=True, separators=(",", ":")))
            target.write("\n")
            target.flush()
            os.fsync(target.fileno())

    def _remove_wal(self, snapshot_id: str) -> None:
        path = self._wal_path(snapshot_id)
        try:
            path.unlink()
        except FileNotFoundError:
            return
        self._fsync_directory(path.parent)

    def _recover_staging(self, *, skip_snapshot_ids: set[str] | None = None) -> None:
        """Restore captures left by a process interruption."""

        skip_snapshot_ids = skip_snapshot_ids or set()
        for path in sorted(self._staging_root.glob("*.jsonl")):
            try:
                records = [
                    json.loads(line)
                    for line in path.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
                if not records or records[0].get("type") != "start":
                    continue
                start = records[0]
                snapshot_id = _validate_component(str(start["snapshot_id"]), "snapshot_id")
                if snapshot_id in skip_snapshot_ids:
                    continue
                symbol = _normalise_symbol(str(start["symbol"]))
                capture = SnapshotCapture(
                    snapshot_id=snapshot_id,
                    symbol=symbol,
                    signal_time_ms=int(start["signal_time_ms"]),
                    start_time_ms=int(start["start_time_ms"]),
                    due_time_ms=int(start["due_time_ms"]),
                    campaign_id=start.get("campaign_id"),
                    metadata=start.get("metadata") or {},
                    pre_window_covered=bool(start.get("pre_window_covered", False)),
                    post_window_covered=bool(start.get("post_window_covered", False)),
                )
                finalize_record: tuple[bool, int, bool] | None = None
                for record in records[1:]:
                    if record.get("type") == "bar":
                        if finalize_record is not None:
                            raise ValueError("snapshot WAL contains a bar after finalize")
                        capture.add_bar(_bar_from_dict(record["bar"]))
                    elif record.get("type") == "finalize":
                        recovered_finalize = (
                            bool(record["complete"]),
                            int(record["end_time_ms"]),
                            bool(record["post_window_covered"]),
                        )
                        if (
                            finalize_record is not None
                            and recovered_finalize != finalize_record
                        ):
                            raise ValueError(
                                "snapshot WAL contains conflicting finalize records"
                            )
                        finalize_record = recovered_finalize
                if finalize_record is not None:
                    (
                        capture.finalizing_complete,
                        capture.finalizing_end_time_ms,
                        capture.finalizing_post_window_covered,
                    ) = finalize_record
                if snapshot_id not in self._active:
                    self._active[snapshot_id] = capture
            except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
                # Leave malformed WALs in place for operator inspection.  A
                # bad one must not prevent unrelated symbols from recovering.
                continue

    @staticmethod
    def _fsync_file(path: Path) -> None:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        fd = os.open(path, flags)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


# A descriptive alias for execution coordinators that call this component a
# manager.  Keeping one implementation avoids two subtly different windows.
LiveSnapshotManager = LiveSnapshotStore


def read_campaign_snapshot_candles(
    path: str | Path,
    *,
    symbol: str,
    interval: str = SNAPSHOT_TIMEFRAME,
    start_ms: int | None = None,
    end_ms: int | None = None,
) -> list[dict[str, Any]]:
    """Read the API representation of a persisted campaign 1s snapshot.

    The Ledger API intentionally exposes only ``interval='1s'`` for these
    immutable live payloads.  The service continues to use Binance for 1m+
    chart data; this helper does not turn the snapshot into a second history
    source.  It is kept as a small function so Ledger can lazy-import it
    without constructing a market service.
    """

    if interval != SNAPSHOT_TIMEFRAME:
        raise ValueError("campaign snapshots only support interval='1s'")
    normalised_symbol = _normalise_symbol(symbol)
    target = Path(path).resolve()
    if target.suffix != ".parquet" or not target.is_file():
        raise FileNotFoundError(target)
    rows = sorted(
        pq.read_table(target).to_pylist(),
        key=lambda row: int(row["timestamp"]),
    )
    result: list[dict[str, Any]] = []
    for row in rows:
        if _normalise_symbol(str(row["symbol"])) != normalised_symbol:
            raise ValueError("snapshot symbol does not match the requested symbol")
        timestamp = int(row["timestamp"])
        if start_ms is not None and timestamp < int(start_ms):
            continue
        if end_ms is not None and timestamp >= int(end_ms):
            continue
        result.append(
            {
                "time": timestamp // 1_000,
                "open": _api_number(row["open"]),
                "high": _api_number(row["high"]),
                "low": _api_number(row["low"]),
                "close": _api_number(row["close"]),
                "volume": _api_number(row["volume"]),
                "vwap": _api_number(row["vwap"]),
                "quote_volume": _api_number(row["quote_volume"]),
                "trade_count": row["trade_count"],
                "raw_trade_count": row["raw_trade_count"],
                "taker_buy_volume": _api_number(row["taker_buy_volume"]),
                "taker_sell_volume": _api_number(row["taker_sell_volume"]),
                "taker_buy_quote_volume": _api_number(row["taker_buy_quote_volume"]),
                "taker_sell_quote_volume": _api_number(row["taker_sell_quote_volume"]),
                "taker_buy_trade_count": row["taker_buy_trade_count"],
                "taker_sell_trade_count": row["taker_sell_trade_count"],
                "taker_buy_agg_trade_count": row["taker_buy_agg_trade_count"],
                "taker_sell_agg_trade_count": row["taker_sell_agg_trade_count"],
                "max_agg_trade_quantity": _api_number(row["max_agg_trade_quantity"]),
                "max_taker_buy_agg_trade_quantity": _api_number(
                    row["max_taker_buy_agg_trade_quantity"]
                ),
                "max_taker_sell_agg_trade_quantity": _api_number(
                    row["max_taker_sell_agg_trade_quantity"]
                ),
                "first_aggregate_trade_id": row["first_aggregate_trade_id"],
                "last_aggregate_trade_id": row["last_aggregate_trade_id"],
                "first_trade_id": row["first_trade_id"],
                "last_trade_id": row["last_trade_id"],
            }
        )
    return result


def _api_number(value: Any) -> Any:
    """Convert Arrow decimals at the JSON boundary without losing parquet precision."""

    return float(value) if isinstance(value, Decimal) else value


def _id_bounds(rows: Sequence[Mapping[str, Any]], column: str) -> tuple[int | None, int | None]:
    values = [row.get(column) for row in rows if row.get(column) is not None]
    if not values:
        return None, None
    return min(int(value) for value in values), max(int(value) for value in values)


def _continuity(
    rows: Sequence[Mapping[str, Any]],
    *,
    start_time_ms: int | None,
    end_time_ms: int | None,
) -> tuple[
    tuple[int, ...],
    tuple[int, ...],
    tuple[int, ...],
    tuple[tuple[int, int], ...],
    tuple[tuple[int, int], ...],
]:
    """Separate no-trade seconds, true gaps, and unobserved boundaries.

    A missing row between two bars is only considered a valid empty second
    when the aggregate-trade IDs on either side are contiguous.  Boundary
    seconds cannot be proved empty and are therefore reported as incomplete
    coverage instead of as a stream gap.
    """

    expected_start = _ceil_second(start_time_ms) if start_time_ms is not None else None
    expected_end = _ceil_second(end_time_ms) if end_time_ms is not None else None
    ordered = sorted(rows, key=lambda row: int(row["timestamp"]))
    timestamps = [int(row["timestamp"]) for row in ordered]
    expected = (
        tuple(range(expected_start, expected_end, 1_000))
        if expected_start is not None and expected_end is not None and expected_end > expected_start
        else ()
    )
    if not ordered:
        return (), (), expected, (), ()

    expected_set = set(expected)
    present = set(timestamps)
    boundary = tuple(
        timestamp
        for timestamp in expected
        if timestamp < timestamps[0] or timestamp > timestamps[-1]
    )
    if expected_start is None or expected_end is None:
        interior_candidates = tuple(
            timestamp
            for timestamp in range(timestamps[0], timestamps[-1] + 1_000, 1_000)
            if timestamp not in present
        )
    else:
        interior_candidates = tuple(
            timestamp
            for timestamp in expected
            if timestamp in expected_set
            and timestamps[0] < timestamp < timestamps[-1]
            and timestamp not in present
        )

    empty: list[int] = []
    missing: list[int] = []
    gaps: list[tuple[int, int]] = []
    non_monotonic: list[tuple[int, int]] = []
    for previous, current in zip(ordered, ordered[1:]):
        previous_timestamp = int(previous["timestamp"])
        current_timestamp = int(current["timestamp"])
        between = tuple(
            timestamp
            for timestamp in interior_candidates
            if previous_timestamp < timestamp < current_timestamp
        )
        previous_first = previous.get("first_aggregate_trade_id")
        previous_last = previous.get("last_aggregate_trade_id")
        current_first = current.get("first_aggregate_trade_id")
        current_last = current.get("last_aggregate_trade_id")
        ids_available = all(
            value is not None
            for value in (previous_last, current_first, current_last)
        )
        if ids_available:
            previous_last_id = int(previous_last)
            current_first_id = int(current_first)
            current_last_id = int(current_last)
            if current_first_id > previous_last_id + 1:
                gaps.append((previous_last_id + 1, current_first_id - 1))
            elif current_first_id <= previous_last_id:
                # Aggregate trade IDs are globally monotonic for a symbol.  An
                # overlap therefore proves duplicated, regressed or otherwise
                # corrupted input even when no candle second is absent.
                non_monotonic.append(
                    (previous_last_id + 1, current_first_id)
                )
            if current_first_id <= current_last_id and current_first_id == previous_last_id + 1:
                empty.extend(between)
            else:
                missing.extend(between)
        else:
            missing.extend(between)

    # A malformed individual bar is itself an aggregate-ID gap.  Do not use
    # it to infer that surrounding missing seconds were no-trade periods.
    for row in ordered:
        first = row.get("first_aggregate_trade_id")
        last = row.get("last_aggregate_trade_id")
        if first is None or last is None:
            continue
        first_id = int(first)
        last_id = int(last)
        if first_id > last_id:
            gaps.append((last_id, first_id))

    return (
        tuple(sorted(set(empty))),
        tuple(sorted(set(missing))),
        tuple(sorted(set(boundary))),
        tuple(gaps),
        tuple(non_monotonic),
    )


def _covers_start(rows: Sequence[Mapping[str, Any]], start_time_ms: int | None) -> bool:
    if start_time_ms is None:
        return True
    start = _ceil_second(start_time_ms)
    return bool(rows) and min(int(row["timestamp"]) for row in rows) <= start


def _covers_end(rows: Sequence[Mapping[str, Any]], end_time_ms: int | None) -> bool:
    if end_time_ms is None:
        return True
    start = _ceil_second(end_time_ms)
    if start <= 0:
        return True
    return bool(rows) and max(int(row["timestamp"]) for row in rows) >= start - 1_000


def _ceil_second(timestamp_ms: int) -> int:
    value = int(timestamp_ms)
    return (value + 999) // 1_000 * 1_000


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = [
    "DEFAULT_POST_WINDOW_MS",
    "DEFAULT_PRE_WINDOW_MS",
    "LiveSnapshotManager",
    "LiveSnapshotStore",
    "SNAPSHOT_COLUMNS",
    "SNAPSHOT_AGGREGATION_VERSION",
    "SNAPSHOT_SCHEMA",
    "SNAPSHOT_SCHEMA_VERSION",
    "SnapshotCapture",
    "SnapshotManifest",
    "read_campaign_snapshot_candles",
]
