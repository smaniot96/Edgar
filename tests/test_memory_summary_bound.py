"""Process-wide bound on memory-summary jobs (EDG-158), as pure unit tests.

No Docker, database or provider. The DB snapshot and publish steps are replaced by fakes that
keep the same contract as the real ones: a snapshot is taken only from the narration committed
so far, and a publish is obsolete unless it advances coverage. A fake "connection" is counted
inside the snapshot step, the only place a job touches the database; a job still waiting for a
slot must never open one. The DB-backed races stay in tests/api/test_session_memory.py.
"""

import asyncio
import os
from contextlib import suppress
from dataclasses import dataclass, field

import pytest

from apps.api.services import session_messages as memory


@dataclass
class FakeStore:
    narrations: dict[int, int] = field(default_factory=dict)  # session -> committed turns
    covered: dict[int, int] = field(default_factory=dict)  # session -> published coverage
    published: list[tuple[int, int]] = field(default_factory=list)
    snapshots: list[int] = field(default_factory=list)
    open_connections: int = 0
    max_open_connections: int = 0
    in_flight: int = 0
    max_in_flight: int = 0
    gate: asyncio.Event | None = None  # when set, summarize blocks until released
    summarize_delay: float = 0.0


class _FakeConnection:
    def __init__(self, store: FakeStore):
        self.store = store

    async def __aenter__(self):
        self.store.open_connections += 1
        self.store.max_open_connections = max(
            self.store.max_open_connections, self.store.open_connections
        )
        return self

    async def __aexit__(self, *_exc):
        self.store.open_connections -= 1


@pytest.fixture
def store(monkeypatch) -> FakeStore:
    fake = FakeStore()

    async def read_snapshot(session_id, *, min_turns):
        async with _FakeConnection(fake):
            fake.snapshots.append(session_id)
            total = fake.narrations.get(session_id, 0)
            base = fake.covered.get(session_id, 0)
            if total <= base:
                return None
            pending = range(base + 1, total + 1)
            return memory._MemorySnapshot(
                base_summary_id=None,
                previous_summary=None,
                through_event_id=total,
                turns_summarized=len(pending),
                messages=tuple(("ai", f"turn {i}") for i in pending),
            )

    async def publish(session_id, snapshot, summary):
        if fake.covered.get(session_id, 0) >= snapshot.through_event_id:
            return "obsolete"
        fake.covered[session_id] = snapshot.through_event_id
        fake.published.append((session_id, snapshot.through_event_id))
        return "published"

    async def summarize(previous, messages):
        fake.in_flight += 1
        fake.max_in_flight = max(fake.max_in_flight, fake.in_flight)
        try:
            if fake.gate is not None:
                await fake.gate.wait()
            if fake.summarize_delay:
                await asyncio.sleep(fake.summarize_delay)
            return " ".join(m.content for m in messages)
        finally:
            fake.in_flight -= 1

    monkeypatch.setattr(memory, "_read_memory_snapshot", read_snapshot)
    monkeypatch.setattr(memory, "_publish_memory_summary", publish)
    monkeypatch.setattr(memory, "summarize_story", summarize)
    return fake


@pytest.fixture
def bound(monkeypatch):
    """Install a fresh slot pool and waiter set per test; returns a function to set the bound."""

    def install(size: int) -> None:
        monkeypatch.setattr(memory, "_summary_slots", asyncio.Semaphore(size))
        monkeypatch.setattr(memory, "_waiting_sessions", set())

    install(2)
    return install


async def _settle(rounds: int = 10) -> None:
    for _ in range(rounds):
        await asyncio.sleep(0)


def test_bound_comes_from_the_documented_setting_with_a_default_of_two():
    from edgar_core.config import MEMORY_SUMMARY_CONCURRENCY

    expected = max(1, int(os.environ.get("EDGAR_MEMORY_SUMMARY_CONCURRENCY", "2")))
    assert MEMORY_SUMMARY_CONCURRENCY == expected


async def test_running_jobs_never_exceed_the_bound(store, bound):
    bound(2)
    for sid in range(1, 9):
        store.narrations[sid] = 1
    store.summarize_delay = 0.01

    await asyncio.wait_for(
        asyncio.gather(*(memory.maybe_update_memory_summary(sid) for sid in range(1, 9))),
        timeout=5,
    )

    assert store.max_in_flight == 2
    assert sorted(store.published) == [(sid, 1) for sid in range(1, 9)]
    assert store.open_connections == 0


async def test_waiting_job_holds_no_connection_until_it_gets_a_slot(store, bound):
    bound(1)
    store.narrations = {1: 1, 2: 1}
    store.gate = asyncio.Event()

    first = asyncio.create_task(memory.maybe_update_memory_summary(1))
    await _settle()
    second = asyncio.create_task(memory.maybe_update_memory_summary(2))
    await _settle()

    assert store.snapshots == [1]  # the second job has not started its snapshot
    assert store.open_connections == 0
    assert not second.done()

    store.gate.set()
    await asyncio.wait_for(asyncio.gather(first, second), timeout=5)
    assert store.snapshots == [1, 2]
    assert store.max_open_connections == 1
    assert sorted(store.published) == [(1, 1), (2, 1)]


async def test_cancelled_waiter_leaves_the_queue_and_cancelled_runner_frees_its_slot(store, bound):
    bound(1)
    store.narrations = {1: 1, 2: 1, 3: 1}
    store.gate = asyncio.Event()

    running = asyncio.create_task(memory.maybe_update_memory_summary(1))
    await _settle()
    waiting = asyncio.create_task(memory.maybe_update_memory_summary(2))
    await _settle()
    waiting.cancel()
    with suppress(asyncio.CancelledError):
        await waiting
    assert 2 not in memory._waiting_sessions
    assert 2 not in store.snapshots

    running.cancel()
    with suppress(asyncio.CancelledError):
        await running
    assert memory._summary_slots._value == 1
    assert store.open_connections == 0

    # The freed slot admits the next job; a leaked slot would hang here.
    store.gate.set()
    await asyncio.wait_for(memory.maybe_update_memory_summary(3), timeout=2)
    assert (3, 1) in store.published


async def test_trigger_skipped_while_a_job_waits_is_covered_by_that_job(store, bound):
    bound(1)
    store.narrations = {1: 1}
    store.gate = asyncio.Event()

    running = asyncio.create_task(memory.maybe_update_memory_summary(1))
    await _settle()
    store.narrations[1] = 2  # a turn commits while the first job runs
    queued = asyncio.create_task(memory.maybe_update_memory_summary(1))
    await _settle()
    store.narrations[1] = 3  # a later turn commits while the job is still queued
    skipped = asyncio.create_task(memory.maybe_update_memory_summary(1))
    await asyncio.wait_for(skipped, timeout=2)  # returns at once, without a snapshot

    store.gate.set()
    await asyncio.wait_for(asyncio.gather(running, queued), timeout=5)

    assert store.snapshots == [1, 1]  # the skipped trigger never started a job
    assert store.published == [(1, 1), (1, 3)]  # one row per job, newest coverage last
    assert store.covered[1] == 3
    assert store.max_in_flight == 1


async def test_queued_job_after_publish_never_regresses_coverage(store, bound):
    bound(1)
    store.narrations = {1: 2}
    await memory.maybe_update_memory_summary(1)
    store.narrations[1] = 3
    await memory.maybe_update_memory_summary(1)
    await memory.maybe_update_memory_summary(1)  # nothing new: no snapshot, no duplicate
    assert store.published == [(1, 2), (1, 3)]
    assert store.snapshots == [1, 1, 1]  # the last job found nothing new and published nothing
