from __future__ import annotations

import asyncio
import json
import os
import stat
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from trading_platform.shared.execution_event_journal import (
    DurableExecutionEventJournal,
    ExecutionEvent,
    redact_event_details,
)


class SequentialIds:
    def __init__(self) -> None:
        self.value = 0

    def __call__(self) -> str:
        self.value += 1
        return f"event-{self.value}"


def make_event(**overrides: object) -> ExecutionEvent:
    values: dict[str, object] = {
        "event_id": "event-1",
        "run_id": "run-1",
        "sequence": 1,
        "event_time": 1_700_000_000_000,
        "event_type": "order.intent_recorded",
        "source": "spike.execution",
        "severity": "info",
        "account_id": "account-1",
        "strategy_id": "spike_short",
        "trace_id": "trace-1",
        "causation_id": None,
        "symbol": "BTCUSDT",
        "campaign_id": "campaign-1",
        "client_order_id": "client-1",
        "exchange_order_id": None,
        "details": {},
    }
    values.update(overrides)
    return ExecutionEvent(**values)  # type: ignore[arg-type]


def test_execution_event_roundtrip_is_strict_and_json_canonical() -> None:
    event = make_event(
        details={
            "quantity": Decimal("1.2300"),
            "observed_at": datetime(
                2026, 9, 6, 16, 30, tzinfo=timezone(timedelta(hours=8))
            ),
            "flags": (True, None),
        }
    )

    assert event.details == {
        "quantity": "1.2300",
        "observed_at": "2026-09-06T08:30:00Z",
        "flags": [True, None],
    }
    assert ExecutionEvent.from_dict(json.loads(event.to_json())) == event

    missing = event.to_dict()
    missing.pop("source")
    with pytest.raises(ValueError, match="missing fields"):
        ExecutionEvent.from_dict(missing)

    extra = event.to_dict()
    extra["unexpected"] = True
    with pytest.raises(ValueError, match="unknown fields"):
        ExecutionEvent.from_dict(extra)

    with pytest.raises(ValueError, match="timezone"):
        make_event(details={"observed_at": datetime(2026, 9, 6)})


def test_recursive_redaction_does_not_modify_input() -> None:
    details = {
        "headers": {"Authorization": "Bearer secret", "request_id": "request-1"},
        "items": [
            {
                "api_key": "key",
                "api-secret": "secret",
                "signature": "signature",
                "listenKey": "listen-key",
                "password": "password",
                "refresh_token": "token",
            }
        ],
    }

    redacted = redact_event_details(details)

    assert details["headers"]["Authorization"] == "Bearer secret"
    assert details["items"][0]["api_key"] == "key"
    assert redacted == {
        "headers": {"Authorization": "[REDACTED]", "request_id": "request-1"},
        "items": [
            {
                "api_key": "[REDACTED]",
                "api-secret": "[REDACTED]",
                "signature": "[REDACTED]",
                "listenKey": "[REDACTED]",
                "password": "[REDACTED]",
                "refresh_token": "[REDACTED]",
            }
        ],
    }


@pytest.mark.asyncio
async def test_append_fsyncs_local_record_before_persist_callback(tmp_path) -> None:
    path = tmp_path / "execution-events.jsonl"
    observations: list[tuple[ExecutionEvent, ...]] = []

    async def persist(events: tuple[ExecutionEvent, ...]) -> int:
        disk = [json.loads(line) for line in path.read_text().splitlines()]
        assert disk[-1] == events[-1].to_dict()
        observations.append(events)
        return len(events)

    journal = DurableExecutionEventJournal(
        path,
        "run-1",
        "account-1",
        "spike_short",
        persist,
        now_ms=lambda: 123,
        id_factory=SequentialIds(),
    )

    event = await journal.append(
        "order.intent_recorded",
        source="spike.execution",
        symbol="BTCUSDT",
        details={"api_secret": "must-not-leak", "quantity": Decimal("2.5")},
    )

    assert event.sequence == 1
    assert event.event_time == 123
    assert event.trace_id == event.event_id
    assert event.details == {"api_secret": "[REDACTED]", "quantity": "2.5"}
    assert observations == [(event,)]


@pytest.mark.asyncio
async def test_first_append_fsyncs_file_and_parent_directory(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "nested" / "execution-events.jsonl"
    fsync_targets: list[str] = []
    real_fsync = os.fsync

    def recording_fsync(fd: int) -> None:
        mode = os.fstat(fd).st_mode
        fsync_targets.append("directory" if stat.S_ISDIR(mode) else "file")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", recording_fsync)

    async def persist(events: tuple[ExecutionEvent, ...]) -> int:
        return len(events)

    journal = DurableExecutionEventJournal(
        path,
        "run-1",
        "account-1",
        "spike_short",
        persist,
        id_factory=SequentialIds(),
    )

    await journal.append("first", source="test")
    assert fsync_targets == ["file", "directory", "file", "directory"]

    fsync_targets.clear()
    await journal.append("second", source="test")
    assert fsync_targets == ["file", "file", "directory"]


@pytest.mark.asyncio
async def test_persist_failure_keeps_file_and_start_replays_same_event(tmp_path) -> None:
    path = tmp_path / "execution-events.jsonl"

    async def failing_persist(events: tuple[ExecutionEvent, ...]) -> int:
        raise RuntimeError("database unavailable")

    journal = DurableExecutionEventJournal(
        path,
        "run-1",
        "account-1",
        "spike_short",
        failing_persist,
        id_factory=SequentialIds(),
    )
    with pytest.raises(RuntimeError, match="database unavailable"):
        await journal.append("order.submit_started", source="spike.execution")

    disk_event = ExecutionEvent.from_dict(json.loads(path.read_text().strip()))
    replayed: list[ExecutionEvent] = []

    async def recovering_persist(events: tuple[ExecutionEvent, ...]) -> int:
        replayed.extend(events)
        return len(events)

    recovered = DurableExecutionEventJournal(
        path,
        "run-1",
        "account-1",
        "spike_short",
        recovering_persist,
        id_factory=lambda: "event-2",
    )
    assert await recovered.start() == 1
    assert replayed == [disk_event]
    assert path.read_text() == ""

    next_event = await recovered.append("order.submit_retried", source="spike.execution")
    assert next_event.sequence == 2


@pytest.mark.asyncio
async def test_append_after_persist_failure_replays_all_pending_events(tmp_path) -> None:
    path = tmp_path / "execution-events.jsonl"
    calls: list[tuple[ExecutionEvent, ...]] = []
    should_fail = True

    async def persist(events: tuple[ExecutionEvent, ...]) -> int:
        nonlocal should_fail
        calls.append(events)
        if should_fail:
            should_fail = False
            raise RuntimeError("database unavailable")
        return len(events)

    journal = DurableExecutionEventJournal(
        path,
        "run-1",
        "account-1",
        "spike_short",
        persist,
        id_factory=SequentialIds(),
    )

    with pytest.raises(RuntimeError, match="database unavailable"):
        await journal.append("order.submit_started", source="spike.execution")

    second = await journal.append("order.submit_retried", source="spike.execution")

    assert second.sequence == 2
    assert path.read_text() == ""
    assert [[event.sequence for event in batch] for batch in calls] == [[1], [1, 2]]


@pytest.mark.asyncio
async def test_successful_persist_compacts_wal_even_when_nothing_is_inserted(tmp_path) -> None:
    path = tmp_path / "execution-events.jsonl"
    calls: list[tuple[ExecutionEvent, ...]] = []

    async def persist(events: tuple[ExecutionEvent, ...]) -> int:
        calls.append(events)
        return 0

    journal = DurableExecutionEventJournal(
        path,
        "run-1",
        "account-1",
        "spike_short",
        persist,
        id_factory=SequentialIds(),
    )

    await journal.append("already-in-postgres", source="test")

    assert path.read_text() == ""
    assert calls[0][0].event_type == "already-in-postgres"


@pytest.mark.asyncio
async def test_compaction_failure_keeps_wal_for_idempotent_replay(tmp_path, monkeypatch) -> None:
    path = tmp_path / "execution-events.jsonl"
    persisted: list[tuple[ExecutionEvent, ...]] = []

    async def persist(events: tuple[ExecutionEvent, ...]) -> int:
        persisted.append(events)
        return 1

    journal = DurableExecutionEventJournal(
        path,
        "run-1",
        "account-1",
        "spike_short",
        persist,
        id_factory=SequentialIds(),
    )

    def fail_compaction() -> None:
        raise OSError("WAL compaction failed")

    monkeypatch.setattr(journal, "_compact_local_sync", fail_compaction)
    with pytest.raises(OSError, match="WAL compaction failed"):
        await journal.append("persisted-before-compaction", source="test")

    event = ExecutionEvent.from_dict(json.loads(path.read_text().strip()))
    assert persisted == [(event,)]

    replayed: list[tuple[ExecutionEvent, ...]] = []

    async def retry(events: tuple[ExecutionEvent, ...]) -> int:
        replayed.append(events)
        return 0

    recovered = DurableExecutionEventJournal(
        path,
        "run-2",
        "account-1",
        "spike_short",
        retry,
        id_factory=lambda: "event-2",
    )
    assert await recovered.start() == 0
    assert replayed == [(event,)]
    assert path.read_text() == ""


@pytest.mark.asyncio
async def test_restarts_replay_only_unconfirmed_wal_until_successful_compaction(
    tmp_path,
) -> None:
    path = tmp_path / "execution-events.jsonl"
    attempts = 0
    calls: list[tuple[ExecutionEvent, ...]] = []

    async def persist(events: tuple[ExecutionEvent, ...]) -> int:
        nonlocal attempts
        attempts += 1
        calls.append(events)
        if attempts in {2, 3}:
            raise RuntimeError("database unavailable")
        return 0

    first = DurableExecutionEventJournal(
        path,
        "run-1",
        "account-1",
        "spike_short",
        persist,
        id_factory=SequentialIds(),
    )
    await first.append("confirmed", source="test")
    with pytest.raises(RuntimeError, match="database unavailable"):
        await first.append("unconfirmed", source="test")

    failed_restart = DurableExecutionEventJournal(
        path,
        "run-2",
        "account-1",
        "spike_short",
        persist,
        id_factory=lambda: "event-3",
    )
    with pytest.raises(RuntimeError, match="database unavailable"):
        await failed_restart.start()

    successful_restart = DurableExecutionEventJournal(
        path,
        "run-3",
        "account-1",
        "spike_short",
        persist,
        id_factory=lambda: "event-4",
    )
    assert await successful_restart.start() == 0
    assert path.read_text() == ""

    final_calls: list[tuple[ExecutionEvent, ...]] = []

    async def unexpected_replay(events: tuple[ExecutionEvent, ...]) -> int:
        final_calls.append(events)
        return 0

    final_restart = DurableExecutionEventJournal(
        path,
        "run-4",
        "account-1",
        "spike_short",
        unexpected_replay,
    )
    assert await final_restart.start() == 0
    assert final_calls == []
    assert [
        [event.event_type for event in batch]
        for batch in calls
    ] == [["confirmed"], ["unconfirmed"], ["unconfirmed"], ["unconfirmed"]]


@pytest.mark.asyncio
async def test_explicit_start_retries_pending_events_after_database_failure(tmp_path) -> None:
    path = tmp_path / "execution-events.jsonl"
    event = make_event()
    path.write_text(event.to_json() + "\n")
    calls: list[tuple[ExecutionEvent, ...]] = []

    async def persist(events: tuple[ExecutionEvent, ...]) -> int:
        calls.append(events)
        raise RuntimeError("database unavailable")

    journal = DurableExecutionEventJournal(
        path, "run-1", "account-1", "spike_short", persist
    )

    with pytest.raises(RuntimeError, match="database unavailable"):
        await journal.start()
    with pytest.raises(RuntimeError, match="database unavailable"):
        await journal.start()

    assert calls == [(event,), (event,)]


@pytest.mark.asyncio
async def test_concurrent_appends_have_contiguous_unique_sequences(tmp_path) -> None:
    persisted: list[ExecutionEvent] = []

    async def persist(events: tuple[ExecutionEvent, ...]) -> int:
        await asyncio.sleep(0)
        persisted.extend(events)
        return len(events)

    journal = DurableExecutionEventJournal(
        tmp_path / "events.jsonl",
        "run-1",
        "account-1",
        "spike_short",
        persist,
        id_factory=SequentialIds(),
    )
    events = await asyncio.gather(
        *(
            journal.append(f"event-{index}", source="test")
            for index in range(25)
        )
    )

    assert sorted(event.sequence for event in events) == list(range(1, 26))
    assert [event.sequence for event in persisted] == list(range(1, 26))
    assert len({event.event_id for event in persisted}) == 25


@pytest.mark.asyncio
async def test_start_replays_unconfirmed_old_run_and_new_run_starts_at_one(tmp_path) -> None:
    path = tmp_path / "events.jsonl"

    async def failing_persist(events: tuple[ExecutionEvent, ...]) -> int:
        raise RuntimeError("database unavailable")

    first = DurableExecutionEventJournal(
        path,
        "run-1",
        "account-1",
        "spike_short",
        failing_persist,
        id_factory=SequentialIds(),
    )
    with pytest.raises(RuntimeError, match="database unavailable"):
        await first.append("run-one", source="test")

    replay_batches: list[tuple[ExecutionEvent, ...]] = []

    async def collect(events: tuple[ExecutionEvent, ...]) -> int:
        replay_batches.append(events)
        return len(events)

    second = DurableExecutionEventJournal(
        path,
        "run-2",
        "account-1",
        "spike_short",
        collect,
        id_factory=lambda: "run-2-event-1",
    )
    assert await second.start() == 1
    assert path.read_text() == ""
    run_two_event = await second.append("run-two", source="test")
    assert run_two_event.sequence == 1
    assert [event.run_id for event in replay_batches[0]] == ["run-1"]

    restarted = DurableExecutionEventJournal(
        path,
        "run-3",
        "account-1",
        "spike_short",
        collect,
        id_factory=lambda: "run-3-event-1",
    )
    assert await restarted.start() == 0
    assert (await restarted.append("continued", source="test")).sequence == 1
    assert [event.run_id for event in replay_batches[-1]] == ["run-3"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    [
        "not-json\n",
        "{}\n",
        "\n",
        '{"event_id":"partial"}',
    ],
)
async def test_start_fails_closed_on_invalid_journal(tmp_path, content: str) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text(content)
    called = False

    async def persist(events: tuple[ExecutionEvent, ...]) -> int:
        nonlocal called
        called = True
        return len(events)

    journal = DurableExecutionEventJournal(
        path, "run-1", "account-1", "spike_short", persist
    )
    with pytest.raises(ValueError, match="journal record|JSONL record"):
        await journal.start()
    assert called is False


@pytest.mark.asyncio
async def test_start_rejects_noncontiguous_sequence_within_a_run(tmp_path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text(
        make_event(sequence=1).to_json()
        + "\n"
        + make_event(event_id="event-2", sequence=3).to_json()
        + "\n"
    )

    async def persist(events: tuple[ExecutionEvent, ...]) -> int:
        return len(events)

    journal = DurableExecutionEventJournal(
        path, "run-1", "account-1", "spike_short", persist
    )
    with pytest.raises(ValueError, match="not contiguous"):
        await journal.start()
