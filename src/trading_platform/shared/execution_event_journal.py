"""Durable PostgreSQL-front WAL for execution events.

The journal writes and fsyncs a local JSONL record before invoking the
application's PostgreSQL persistence callback.  Once the callback succeeds,
the confirmed WAL is durably compacted.  A new journal instance replays only
records left in the WAL after an interrupted or failed persistence attempt.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import math
import os
import re
import tempfile
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any


__all__ = [
    "ExecutionEvent",
    "DurableExecutionEventJournal",
    "SNAPSHOT_LIFECYCLE_EVENT_TYPES",
    "redact_event_details",
]


# These lifecycle facts can be replayed by a new process after the local
# snapshot outbox has survived a crash.  Their event_id/run_id/sequence are
# process-local, so the database uses the explicit domain key instead.
SNAPSHOT_LIFECYCLE_EVENT_TYPES = frozenset(
    {"market.snapshot_completed", "market.snapshot_failed"}
)


_REDACTED = "[REDACTED]"
_SENSITIVE_KEY_MARKERS = (
    "apikey",
    "secret",
    "signature",
    "authorization",
    "listenkey",
    "password",
    "token",
    "passphrase",
    "credential",
    "cookie",
)


def _normalise_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.casefold())


def _is_sensitive_key(key: str) -> bool:
    normalised = _normalise_key(key)
    return any(marker in normalised for marker in _SENSITIVE_KEY_MARKERS)


def redact_event_details(details: Any) -> Any:
    """Return a recursively redacted copy of ``details``.

    Sensitive values are replaced based on case-insensitive key names.  The
    input containers are never modified.  Unsupported values are left for the
    canonicalizer to reject with a useful type error.
    """

    return _redact_value(details, set())


def _redact_value(value: Any, active: set[int]) -> Any:
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in active:
            raise ValueError("cyclic event details are not supported")
        active.add(identity)
        try:
            result: dict[Any, Any] = {}
            for key, child in value.items():
                if isinstance(key, str) and _is_sensitive_key(key):
                    result[key] = _REDACTED
                else:
                    result[key] = _redact_value(child, active)
            return result
        finally:
            active.remove(identity)
    if isinstance(value, list):
        identity = id(value)
        if identity in active:
            raise ValueError("cyclic event details are not supported")
        active.add(identity)
        try:
            return [_redact_value(item, active) for item in value]
        finally:
            active.remove(identity)
    if isinstance(value, tuple):
        identity = id(value)
        if identity in active:
            raise ValueError("cyclic event details are not supported")
        active.add(identity)
        try:
            return tuple(_redact_value(item, active) for item in value)
        finally:
            active.remove(identity)
    return value


def _canonical_datetime(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("event detail datetimes must include a timezone")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_json_value(value: Any, active: set[int] | None = None) -> Any:
    """Convert supported detail values into JSON values without coercion loss."""

    if active is None:
        active = set()
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("event details contain a non-finite float")
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("event details contain a non-finite Decimal")
        return str(value)
    if isinstance(value, datetime):
        return _canonical_datetime(value)
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in active:
            raise ValueError("cyclic event details are not supported")
        active.add(identity)
        try:
            result: dict[str, Any] = {}
            for key, child in value.items():
                if not isinstance(key, str):
                    raise TypeError("event detail object keys must be strings")
                result[key] = _canonical_json_value(child, active)
            return result
        finally:
            active.remove(identity)
    if isinstance(value, (list, tuple)):
        identity = id(value)
        if identity in active:
            raise ValueError("cyclic event details are not supported")
        active.add(identity)
        try:
            return [_canonical_json_value(item, active) for item in value]
        finally:
            active.remove(identity)
    raise TypeError(
        f"event details contain unsupported value type {type(value).__name__}"
    )


def _normalise_details(details: Mapping[str, Any]) -> dict[str, Any]:
    redacted = redact_event_details(details)
    canonical = _canonical_json_value(redacted)
    if not isinstance(canonical, dict):
        raise TypeError("event details must be a mapping")
    # This also catches NaN/Infinity and keeps the dataclass invariant true if
    # a supported value is added to the canonicalizer in the future.
    json.dumps(
        canonical,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return canonical


def _require_non_empty_string(value: Any, name: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if not value.strip():
        raise ValueError(f"{name} must not be empty")


def _require_optional_string(value: Any, name: str) -> None:
    if value is not None:
        _require_non_empty_string(value, name)


def _require_integer(value: Any, name: str, *, minimum: int | None = None) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")


@dataclass(frozen=True)
class ExecutionEvent:
    event_id: str
    run_id: str
    sequence: int
    event_time: int
    event_type: str
    source: str
    severity: str
    account_id: str
    strategy_id: str
    trace_id: str
    causation_id: str | None
    symbol: str | None
    campaign_id: str | None
    client_order_id: str | None
    exchange_order_id: str | None
    details: dict[str, Any]
    idempotency_key: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "event_id",
            "run_id",
            "event_type",
            "source",
            "severity",
            "account_id",
            "strategy_id",
            "trace_id",
        ):
            _require_non_empty_string(getattr(self, name), name)
        _require_integer(self.sequence, "sequence", minimum=1)
        _require_integer(self.event_time, "event_time", minimum=0)
        for name in (
            "causation_id",
            "symbol",
            "campaign_id",
            "client_order_id",
            "exchange_order_id",
            "idempotency_key",
        ):
            _require_optional_string(getattr(self, name), name)
        if (
            self.idempotency_key is not None
            and self.event_type not in SNAPSHOT_LIFECYCLE_EVENT_TYPES
        ):
            raise ValueError(
                "idempotency_key is only valid for snapshot lifecycle events"
            )
        if not isinstance(self.details, Mapping):
            raise TypeError("details must be a mapping")
        object.__setattr__(self, "details", _normalise_details(self.details))

    def to_dict(self) -> dict[str, Any]:
        """Return a fresh, strictly JSON-normalized representation."""

        payload = {
            "event_id": self.event_id,
            "run_id": self.run_id,
            "sequence": self.sequence,
            "event_time": self.event_time,
            "event_type": self.event_type,
            "source": self.source,
            "severity": self.severity,
            "account_id": self.account_id,
            "strategy_id": self.strategy_id,
            "trace_id": self.trace_id,
            "causation_id": self.causation_id,
            "symbol": self.symbol,
            "campaign_id": self.campaign_id,
            "client_order_id": self.client_order_id,
            "exchange_order_id": self.exchange_order_id,
            "details": _normalise_details(self.details),
        }
        # Keep the canonical payload of legacy/unkeyed events byte-for-byte
        # compatible with records written before domain idempotency existed.
        if self.idempotency_key is not None:
            payload["idempotency_key"] = self.idempotency_key
        return payload

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ExecutionEvent":
        """Build an event while rejecting missing, extra, or mistyped fields."""

        if not isinstance(data, dict):
            raise TypeError("event data must be a dict")
        expected = {
            "event_id",
            "run_id",
            "sequence",
            "event_time",
            "event_type",
            "source",
            "severity",
            "account_id",
            "strategy_id",
            "trace_id",
            "causation_id",
            "symbol",
            "campaign_id",
            "client_order_id",
            "exchange_order_id",
            "details",
        }
        optional = {"idempotency_key"}
        actual = set(data)
        missing = expected - actual
        extra = actual - expected - optional
        if missing:
            raise ValueError(f"event data is missing fields: {sorted(missing)!r}")
        if extra:
            raise ValueError(f"event data has unknown fields: {sorted(extra)!r}")
        if not isinstance(data["details"], dict):
            raise TypeError("details must be a dict")
        return cls(
            event_id=data["event_id"],
            run_id=data["run_id"],
            sequence=data["sequence"],
            event_time=data["event_time"],
            event_type=data["event_type"],
            source=data["source"],
            severity=data["severity"],
            account_id=data["account_id"],
            strategy_id=data["strategy_id"],
            trace_id=data["trace_id"],
            causation_id=data["causation_id"],
            symbol=data["symbol"],
            campaign_id=data["campaign_id"],
            client_order_id=data["client_order_id"],
            exchange_order_id=data["exchange_order_id"],
            details=data["details"],
            idempotency_key=data.get("idempotency_key"),
        )

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )


PersistCallback = Callable[[Sequence[ExecutionEvent]], Awaitable[int]]


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key!r}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


class DurableExecutionEventJournal:
    """Serialize execution event appends and durably mirror them to PostgreSQL."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        run_id: str,
        account_id: str,
        strategy_id: str,
        persist: PersistCallback,
        now_ms: Callable[[], int] | None = None,
        id_factory: Callable[[], str | uuid.UUID] | None = None,
    ) -> None:
        self.path = Path(path)
        _require_non_empty_string(run_id, "run_id")
        _require_non_empty_string(account_id, "account_id")
        _require_non_empty_string(strategy_id, "strategy_id")
        if not callable(persist):
            raise TypeError("persist must be callable")
        self.run_id = run_id
        self.account_id = account_id
        self.strategy_id = strategy_id
        self._persist = persist
        self._now_ms = now_ms or (lambda: time.time_ns() // 1_000_000)
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex)
        self._lock = asyncio.Lock()
        self._local_loaded = False
        self._started = False
        self._next_sequence = 0
        self._event_ids: set[str] = set()
        self._pending_events: list[ExecutionEvent] = []

    async def start(self) -> int:
        """Parse and replay the complete local journal once for this instance."""

        async with self._lock:
            return await self._start_locked()

    async def append(
        self,
        event_type: str,
        *,
        source: str,
        event_time: int | None = None,
        severity: str = "info",
        trace_id: str | None = None,
        causation_id: str | None = None,
        symbol: str | None = None,
        campaign_id: str | None = None,
        client_order_id: str | None = None,
        exchange_order_id: str | None = None,
        details: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> ExecutionEvent:
        async with self._lock:
            await self._ensure_local_loaded_locked()
            event_id = self._new_event_id()
            event = ExecutionEvent(
                event_id=event_id,
                run_id=self.run_id,
                sequence=self._next_sequence + 1,
                event_time=self._event_time(event_time),
                event_type=event_type,
                source=source,
                severity=severity,
                account_id=self.account_id,
                strategy_id=self.strategy_id,
                trace_id=trace_id if trace_id is not None else event_id,
                causation_id=causation_id,
                symbol=symbol,
                campaign_id=campaign_id,
                client_order_id=client_order_id,
                exchange_order_id=exchange_order_id,
                details={} if details is None else details,
                idempotency_key=idempotency_key,
            )
            return (await self._append_events_locked((event,)))[0]

    async def _start_locked(self) -> int:
        await self._ensure_local_loaded_locked()
        if self._started:
            return 0
        return await self._persist_pending_locked()

    async def _ensure_local_loaded_locked(self) -> None:
        if self._local_loaded:
            return
        events = await asyncio.to_thread(self._read_events_sync)
        self._next_sequence = max(
            (event.sequence for event in events if event.run_id == self.run_id),
            default=0,
        )
        self._event_ids = {event.event_id for event in events}
        self._pending_events = list(events)
        self._local_loaded = True
        self._started = False

    async def _persist_pending_locked(self) -> int:
        if not self._pending_events:
            self._started = True
            return 0
        try:
            persisted = await self._persist_events(tuple(self._pending_events))
            await asyncio.to_thread(self._compact_local_sync)
        except BaseException:
            self._started = False
            raise
        self._pending_events.clear()
        self._started = True
        return persisted

    async def _append_events_locked(
        self, events: Sequence[ExecutionEvent]
    ) -> tuple[ExecutionEvent, ...]:
        if not events:
            return ()
        ids = [event.event_id for event in events]
        if len(set(ids)) != len(ids) or self._event_ids.intersection(ids):
            raise ValueError("event_id must be unique within the journal")
        expected_sequence = self._next_sequence + 1
        for offset, event in enumerate(events):
            if event.sequence != expected_sequence + offset:
                raise ValueError("event sequence is not contiguous")
        lines = "".join(event.to_json() + "\n" for event in events)
        try:
            await asyncio.to_thread(self._append_local_sync, lines)
        except BaseException:
            # A write may have completed partially before an OS error or task
            # cancellation.  Force a complete parse before the next append.
            self._local_loaded = False
            self._started = False
            raise
        self._next_sequence = events[-1].sequence
        self._event_ids.update(ids)
        self._pending_events.extend(events)
        await self._persist_pending_locked()
        return tuple(events)

    async def _persist_events(self, events: Sequence[ExecutionEvent]) -> int:
        result = self._persist(events)
        if not inspect.isawaitable(result):
            raise TypeError("persist must return an awaitable integer")
        count = await result
        if isinstance(count, bool) or not isinstance(count, int):
            raise TypeError("persist must resolve to an integer")
        if count < 0 or count > len(events):
            raise ValueError("persist count must be between zero and the batch size")
        return count

    def _new_event_id(self) -> str:
        value = self._id_factory()
        if isinstance(value, uuid.UUID):
            value = value.hex
        _require_non_empty_string(value, "event_id")
        return value

    def _event_time(self, value: int | None) -> int:
        timestamp = self._now_ms() if value is None else value
        _require_integer(timestamp, "event_time", minimum=0)
        return timestamp

    def _append_local_sync(self, lines: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        file_existed = self.path.exists()
        with self.path.open("a", encoding="utf-8", newline="") as stream:
            stream.write(lines)
            stream.flush()
            os.fsync(stream.fileno())
        if not file_existed:
            directory_fd = os.open(
                self.path.parent,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)

    def _compact_local_sync(self) -> None:
        """Atomically replace the confirmed WAL with an empty durable file."""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_fd, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            dir=self.path.parent,
        )
        temporary_path = Path(temporary_name)
        try:
            stream = os.fdopen(temporary_fd, "w", encoding="utf-8", newline="")
            temporary_fd = -1
            with stream:
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, self.path)
            directory_fd = os.open(
                self.path.parent,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except BaseException:
            if temporary_fd >= 0:
                try:
                    os.close(temporary_fd)
                except OSError:
                    pass
            try:
                temporary_path.unlink()
            except OSError:
                pass
            raise

    def _read_events_sync(self) -> list[ExecutionEvent]:
        if not self.path.exists():
            return []
        events: list[ExecutionEvent] = []
        event_ids: set[str] = set()
        previous_sequences: dict[str, int] = {}
        try:
            with self.path.open("r", encoding="utf-8", newline="") as stream:
                for line_number, line in enumerate(stream, 1):
                    if not line.endswith("\n"):
                        raise ValueError("unterminated JSONL record")
                    if not line.strip():
                        raise ValueError("blank JSONL record")
                    try:
                        raw = json.loads(
                            line,
                            object_pairs_hook=_reject_duplicate_json_keys,
                            parse_constant=_reject_json_constant,
                        )
                        event = ExecutionEvent.from_dict(raw)
                    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                        raise ValueError(
                            f"Invalid execution event journal record at line {line_number}"
                        ) from exc
                    if (
                        event.account_id != self.account_id
                        or event.strategy_id != self.strategy_id
                    ):
                        raise ValueError(
                            f"Execution event identity mismatch at line {line_number}"
                        )
                    previous_sequence = previous_sequences.get(event.run_id)
                    if (
                        previous_sequence is not None
                        and event.sequence != previous_sequence + 1
                    ):
                        raise ValueError(
                            "Execution event sequence is not contiguous for run "
                            f"{event.run_id!r} at line {line_number}"
                        )
                    if event.event_id in event_ids:
                        raise ValueError(
                            f"Duplicate execution event id at line {line_number}"
                        )
                    previous_sequences[event.run_id] = event.sequence
                    event_ids.add(event.event_id)
                    events.append(event)
        except (OSError, UnicodeError) as exc:
            raise ValueError("Unable to read execution event journal") from exc
        return events
