import asyncio

from trading_platform.shared.execution_event_journal import DurableExecutionEventJournal


async def test_slow_postgres_mirror_does_not_hold_the_append_lock(tmp_path):
    entered = asyncio.Event()
    release = asyncio.Event()
    persisted = []
    async def persist(events):
        entered.set()
        await release.wait()
        persisted.extend(events)
        return len(events)
    journal = DurableExecutionEventJournal(tmp_path / "events.jsonl", "run", "account", "strategy", persist, mirror_async=True)
    await journal.start()
    try:
        first = await journal.append("runtime.started", source="test")
        await asyncio.wait_for(entered.wait(), 1)
        second = await asyncio.wait_for(journal.append("runtime.ready", source="test"), 1)
        assert len(journal.path.read_text().splitlines()) == 2
        release.set()
        await journal.flush()
        assert {event.event_id for event in persisted} == {first.event_id, second.event_id}
        assert journal.pending_count == 0
    finally:
        release.set()
        await journal.close()


async def test_async_audit_not_yet_mirrored_is_replayed_after_restart(tmp_path):
    persisted = []
    async def persist(events):
        persisted.extend(events)
        return len(events)
    path = tmp_path / "events.jsonl"
    first = DurableExecutionEventJournal(path, "old-run", "account", "strategy", persist, mirror_async=True)
    event = await first.append("runtime.started", source="test")
    assert persisted == []
    restarted = DurableExecutionEventJournal(path, "new-run", "account", "strategy", persist)
    assert await restarted.start() == 1
    assert [item.event_id for item in persisted] == [event.event_id]
    assert restarted.pending_count == 0
