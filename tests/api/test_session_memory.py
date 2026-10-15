"""Deterministic memory races against disposable PostgreSQL; no model/provider calls."""

import asyncio
from contextlib import suppress
from dataclasses import replace

import pytest
import pytest_asyncio
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session as SyncSession
from sqlalchemy.util.concurrency import await_only

import db.postgres.session as db_session_module
from apps.api.services import session_messages as memory
from db.postgres.models import Campaign, EventLog, Session, User


@pytest_asyncio.fixture
async def memory_db(db_session, monkeypatch, request):
    # One real pooled connection makes a retained checkout observable and prevents the
    # ordinary NullPool fixture from hiding the generation lifetime regression.
    engine = create_async_engine(
        db_session_module.engine.url,
        pool_size=getattr(request, "param", 1),
        max_overflow=0,
        pool_timeout=1,
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_session_module, "async_session_factory", factory)
    async with factory() as db, db.begin():
        user = User(email="memory@example.test")
        db.add(user)
        await db.flush()
        campaign = Campaign(title="Memory", system="5e", created_by=user.id)
        db.add(campaign)
        await db.flush()
        sessions = [Session(campaign_id=campaign.id), Session(campaign_id=campaign.id)]
        db.add_all(sessions)
        await db.flush()
        ids = [s.id for s in sessions]
    yield engine, factory, ids
    await engine.dispose()


async def _narrations(factory, session_id, start, stop):
    async with factory() as db, db.begin():
        rows = [
            EventLog(
                session_id=session_id,
                event_type="narration",
                payload={"player_input": f"p{i}", "narration": f"n{i}"},
            )
            for i in range(start, stop)
        ]
        db.add_all(rows)
        await db.flush()
        return [row.id for row in rows]


async def _rows(factory, session_id, event_type="memory_summary"):
    async with factory() as db:
        return list(
            (
                await db.scalars(
                    select(EventLog)
                    .where(EventLog.session_id == session_id, EventLog.event_type == event_type)
                    .order_by(EventLog.id)
                )
            ).all()
        )


async def _selected(factory, sid):
    async with factory() as db:
        return await memory.load_memory_summary(db, sid)


def _fold(previous, messages):
    # An exact token ledger makes duplicated/dropped input visible across incremental bases.
    return " ".join([previous or "", *(m.content for m in messages if m.type == "ai")]).strip()


async def _wait(event):
    await asyncio.wait_for(event.wait(), timeout=5)


@pytest.mark.parametrize("first_to_finish", ["older", "newer"])
async def test_overlapping_jobs_preserve_max_coverage_and_exact_inputs(
    memory_db, monkeypatch, first_to_finish
):
    engine, factory, (sid, other_sid) = memory_db
    narration_ids = await _narrations(factory, sid, 1, 26)
    original = [(r.id, r.payload) for r in await _rows(factory, sid, "narration")]
    entered = [asyncio.Event(), asyncio.Event()]
    released = [asyncio.Event(), asyncio.Event()]
    calls = []

    async def summarize(previous, messages):
        index = len(calls)
        calls.append((previous, [m.content for m in messages]))
        if index < 2:
            entered[index].set()
            await _wait(released[index])
        return _fold(previous, messages)

    monkeypatch.setattr(memory, "summarize_story", summarize)
    older = asyncio.create_task(memory.maybe_update_memory_summary(sid))
    newer = None
    try:
        await _wait(entered[0])
        assert engine.pool.checkedout() == 0
        # A second session can read/write with the only connection during generation.
        await _narrations(factory, other_sid, 101, 102)
        narration_ids += await _narrations(factory, sid, 26, 31)
        newer = asyncio.create_task(memory.maybe_update_memory_summary(sid))
        await _wait(entered[1])
        assert engine.pool.checkedout() == 0
        first, second = (0, 1) if first_to_finish == "older" else (1, 0)
        tasks = [older, newer]
        released[first].set()
        await asyncio.wait_for(tasks[first], timeout=5)
        assert (await _rows(factory, sid))[-1].payload["through_event_id"] == narration_ids[
            14 if first == 0 else 19
        ]
        released[second].set()
        await asyncio.wait_for(tasks[second], timeout=5)
        summaries = await _rows(factory, sid)
        assert summaries[-1].payload["through_event_id"] == narration_ids[19]
        assert await _selected(factory, sid) == " ".join(f"n{i}" for i in range(1, 21))
        if first_to_finish == "older":
            assert len(calls) == 3
            assert calls[2][0] == " ".join(f"n{i}" for i in range(1, 16))
            assert calls[2][1] == [v for i in range(16, 21) for v in (f"p{i}", f"n{i}")]
            assert summaries[1].payload["base_summary_event_id"] == summaries[0].id
        else:
            assert len(calls) == 2
            assert len(summaries) == 1
        await memory.maybe_update_memory_summary(sid)
        assert len(await _rows(factory, sid)) == len(summaries)
        assert await _selected(factory, other_sid) is None
        assert [(r.id, r.payload) for r in await _rows(factory, sid, "narration")][:25] == original
        assert len(await _rows(factory, sid, "narration")) == 30
    finally:
        for task in (older, newer):
            if task is not None and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task


async def test_equal_target_jobs_and_retries_are_idempotent(memory_db, monkeypatch):
    _, factory, (sid, _) = memory_db
    await _narrations(factory, sid, 1, 26)
    both_entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def summarize(previous, messages):
        nonlocal calls
        calls += 1
        if calls == 2:
            both_entered.set()
        await _wait(release)
        return _fold(previous, messages)

    monkeypatch.setattr(memory, "summarize_story", summarize)
    tasks = [asyncio.create_task(memory.maybe_update_memory_summary(sid)) for _ in range(2)]
    try:
        await _wait(both_entered)
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
        await memory.maybe_update_memory_summary(sid)
        assert calls == 2
        assert len(await _rows(factory, sid)) == 1
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task


async def test_rebase_finishes_remainder_below_batch_threshold(memory_db, monkeypatch):
    _, factory, (sid, _) = memory_db
    ids = await _narrations(factory, sid, 1, 29)  # older target 18
    older = await memory._read_memory_snapshot(sid, min_turns=5)
    ids += await _narrations(factory, sid, 29, 31)  # newer target 20
    calls = 0

    async def summarize(previous, messages):
        nonlocal calls
        calls += 1
        if calls == 1:
            assert (
                await memory._publish_memory_summary(
                    sid, older, " ".join(text for kind, text in older.messages if kind == "ai")
                )
                == "published"
            )
        return _fold(previous, messages)

    monkeypatch.setattr(memory, "summarize_story", summarize)
    await memory.maybe_update_memory_summary(sid)
    assert calls == 2
    assert (await _rows(factory, sid))[-1].payload["through_event_id"] == ids[19]
    assert await _selected(factory, sid) == " ".join(f"n{i}" for i in range(1, 21))


@pytest.mark.parametrize("failure", ["raise", "empty", "cancel"])
async def test_generation_failure_and_cancellation_preserve_previous_summary(
    memory_db, monkeypatch, failure
):
    engine, factory, (sid, _) = memory_db
    await _narrations(factory, sid, 1, 26)
    monkeypatch.setattr(memory, "summarize_story", _async_fold)
    await memory.maybe_update_memory_summary(sid)
    before = [(r.id, r.payload) for r in await _rows(factory, sid)]
    await _narrations(factory, sid, 26, 31)
    entered = asyncio.Event()

    async def summarize(*_args):
        entered.set()
        if failure == "raise":
            raise RuntimeError("synthetic model failure")
        if failure == "empty":
            return " "
        await asyncio.Event().wait()

    monkeypatch.setattr(memory, "summarize_story", summarize)
    task = asyncio.create_task(memory.maybe_update_memory_summary(sid))
    await _wait(entered)
    assert engine.pool.checkedout() == 0
    if failure == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        await asyncio.wait_for(task, timeout=5)
    assert [(r.id, r.payload) for r in await _rows(factory, sid)] == before
    monkeypatch.setattr(memory, "summarize_story", _async_fold)
    await memory.maybe_update_memory_summary(sid)
    assert len(await _rows(factory, sid)) == 2
    assert await _selected(factory, sid) == " ".join(f"n{i}" for i in range(1, 21))


async def _async_fold(previous, messages):
    return _fold(previous, messages)


@pytest.mark.parametrize("failure", ["flush", "commit", "cancel"])
async def test_publication_failure_and_cancellation_roll_back(memory_db, monkeypatch, failure):
    engine, factory, (sid, _) = memory_db
    await _narrations(factory, sid, 1, 26)
    monkeypatch.setattr(memory, "summarize_story", _async_fold)
    entered = asyncio.Event()

    class PublicationSession(SyncSession):
        pass

    failing_factory = async_sessionmaker(
        engine, sync_session_class=PublicationSession, expire_on_commit=False
    )
    monkeypatch.setattr(db_session_module, "async_session_factory", failing_factory)

    def before_commit(db):
        # Ensure a real INSERT occurred before the commit failure, so rollback matters.
        if db.new:
            db.flush()
            raise RuntimeError("synthetic commit failure after insert")

    def after_flush(db, _context):
        if any(isinstance(row, EventLog) and row.event_type == "memory_summary" for row in db.new):
            if failure == "flush":
                raise RuntimeError("synthetic failure after insert")
            entered.set()
            # SQLAlchemy's event runs in the async bridge; pausing after INSERT tests
            # cancellation of the actual publication transaction rather than a stub.
            await_only(asyncio.Event().wait())

    hook_name = "before_commit" if failure == "commit" else "after_flush"
    hook = before_commit if failure == "commit" else after_flush
    event.listen(PublicationSession, hook_name, hook)
    task = asyncio.create_task(memory.maybe_update_memory_summary(sid))
    try:
        if failure == "cancel":
            await _wait(entered)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            await asyncio.wait_for(task, timeout=5)
    finally:
        event.remove(PublicationSession, hook_name, hook)
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
    assert engine.pool.checkedout() == 0
    assert await _rows(factory, sid) == []
    assert len(await _rows(factory, sid, "narration")) == 25
    monkeypatch.setattr(db_session_module, "async_session_factory", factory)
    await memory.maybe_update_memory_summary(sid)
    assert len(await _rows(factory, sid)) == 1


async def test_legacy_selection_validates_coverage_and_preserves_all_rows(memory_db, monkeypatch):
    _, factory, (sid, other_sid) = memory_db
    ids = await _narrations(factory, sid, 1, 26)
    other_ids = await _narrations(factory, other_sid, 101, 102)
    async with factory() as db, db.begin():
        marker = EventLog(session_id=sid, event_type="other", payload={})
        db.add(marker)
        await db.flush()
        payloads = [
            {"summary": "legacy maximum", "through_event_id": str(ids[14])},
            {"summary": "later older", "through_event_id": ids[9]},
            {"summary": "missing boundary"},
            *[
                {"summary": "invalid boundary", "through_event_id": value}
                for value in (
                    None,
                    -1,
                    1.5,
                    True,
                    {},
                    [],
                    "garbage",
                    "9" * 100,
                    999999999,
                    other_ids[0],
                    marker.id,
                )
            ],
            *[
                {"summary": value, "through_event_id": ids[24]}
                for value in (None, "", "  ", "\t\n", {}, [], 123, True)
            ],
        ]
        db.add_all(
            [
                EventLog(session_id=sid, event_type="memory_summary", payload=payload)
                for payload in payloads
            ]
        )
    original = [(r.id, r.payload) for r in await _rows(factory, sid)]
    assert await _selected(factory, sid) == "legacy maximum"
    assert await _selected(factory, other_sid) is None
    snapshot = await memory._read_memory_snapshot(sid, min_turns=1)
    assert snapshot is None  # all 15 aged-out turns already covered
    await _narrations(factory, sid, 26, 31)
    calls = []

    async def summarize(previous, messages):
        calls.append((previous, [m.content for m in messages]))
        return "updated maximum"

    monkeypatch.setattr(memory, "summarize_story", summarize)
    await memory.maybe_update_memory_summary(sid)
    assert calls == [("legacy maximum", [v for i in range(16, 21) for v in (f"p{i}", f"n{i}")])]
    assert await _selected(factory, sid) == "updated maximum"
    assert [(r.id, r.payload) for r in await _rows(factory, sid)][:-1] == original


async def test_unknown_legacy_coverage_falls_back_but_generation_rebuilds(memory_db, monkeypatch):
    _, factory, (sid, _) = memory_db
    await _narrations(factory, sid, 1, 26)
    async with factory() as db, db.begin():
        db.add(
            EventLog(session_id=sid, event_type="memory_summary", payload={"summary": "old text"})
        )
    assert await _selected(factory, sid) == "old text"
    calls = []

    async def summarize(previous, messages):
        calls.append(previous)
        return _fold(previous, messages)

    monkeypatch.setattr(memory, "summarize_story", summarize)
    await memory.maybe_update_memory_summary(sid)
    assert calls == [None]
    assert await _selected(factory, sid) == " ".join(f"n{i}" for i in range(1, 16))
    assert len(await _rows(factory, sid)) == 2


async def test_continuous_changed_bases_bound_generation_and_leave_retryable_history(
    memory_db, monkeypatch
):
    _, factory, (sid, _) = memory_db
    narration_ids = await _narrations(factory, sid, 1, 36)
    calls = 0

    async def summarize(previous, messages):
        nonlocal calls
        calls += 1
        # A competing job advances only five turns each time this job generates.
        snapshot = await memory._read_memory_snapshot(sid, min_turns=1)
        first_five = snapshot.messages[:10]
        competing = replace(
            snapshot,
            through_event_id=narration_ids[calls * 5 - 1],
            turns_summarized=5,
            messages=first_five,
        )
        text = " ".join([previous or "", *(v for k, v in first_five if k == "ai")]).strip()
        assert await memory._publish_memory_summary(sid, competing, text) == "published"
        return _fold(previous, messages)

    monkeypatch.setattr(memory, "summarize_story", summarize)
    await memory.maybe_update_memory_summary(sid)
    assert calls == memory.MAX_GENERATION_ATTEMPTS == 3
    assert len(await _rows(factory, sid)) == 3
    assert len(await _rows(factory, sid, "narration")) == 35
    monkeypatch.setattr(memory, "summarize_story", _async_fold)
    await memory.maybe_update_memory_summary(sid)
    assert await _selected(factory, sid) == " ".join(f"n{i}" for i in range(1, 26))


async def test_legacy_summary_cannot_claim_narration_written_after_it(memory_db):
    _, factory, (sid, _) = memory_db
    ids = await _narrations(factory, sid, 1, 16)
    async with factory() as db, db.begin():
        db.add(
            EventLog(
                session_id=sid,
                event_type="memory_summary",
                payload={"summary": "valid older", "through_event_id": ids[4]},
            )
        )
        impossible = EventLog(
            session_id=sid,
            event_type="memory_summary",
            payload={"summary": "impossible future", "through_event_id": 0},
        )
        db.add(impossible)
        await db.flush()
        future = EventLog(session_id=sid, event_type="narration", payload={"narration": "future"})
        db.add(future)
        await db.flush()
        impossible.payload = {"summary": "impossible future", "through_event_id": future.id}
    assert await _selected(factory, sid) == "valid older"


async def test_summary_loader_preserves_caller_transaction_ownership(memory_db):
    _, factory, (sid, _) = memory_db
    ids = await _narrations(factory, sid, 1, 16)
    async with factory() as db:
        db.add(
            EventLog(
                session_id=sid,
                event_type="memory_summary",
                payload={"summary": "uncommitted", "through_event_id": ids[4]},
            )
        )
        assert await memory.load_memory_summary(db, sid) == "uncommitted"
        assert db.in_transaction()
        await db.rollback()
    assert await _selected(factory, sid) is None


@pytest.mark.parametrize("memory_db", [3], indirect=True)
async def test_publication_lock_serializes_competing_connections(memory_db, monkeypatch):
    engine, factory, (sid, _) = memory_db
    await _narrations(factory, sid, 1, 26)
    snapshot = await memory._read_memory_snapshot(sid, min_turns=5)
    first_read = asyncio.Event()
    release_first = asyncio.Event()
    second_dispatched = asyncio.Event()
    latest = memory._latest_summary_row
    first = None
    second = None
    waiting_pid = None

    async def paused_latest(db, session_id):
        row = await latest(db, session_id)
        if asyncio.current_task() is first:
            first_read.set()
            await _wait(release_first)
        return row

    def before_cursor_execute(connection, _cursor, statement, *_args):
        nonlocal waiting_pid
        if "FOR UPDATE" in statement:
            waiting_pid = connection.connection.driver_connection.get_server_pid()
            second_dispatched.set()

    monkeypatch.setattr(memory, "_latest_summary_row", paused_latest)
    first = asyncio.create_task(memory._publish_memory_summary(sid, snapshot, "winner"))
    try:
        await _wait(first_read)
        event.listen(engine.sync_engine, "before_cursor_execute", before_cursor_execute)
        second = asyncio.create_task(memory._publish_memory_summary(sid, snapshot, "duplicate"))
        await _wait(second_dispatched)
        # A third connection observes the actual PostgreSQL lock wait. No timing guess:
        # the second transaction must be blocked by the first transaction's session lock.
        async with asyncio.timeout(5), engine.connect() as connection:
            while not await connection.scalar(
                text("SELECT cardinality(pg_blocking_pids(:pid)) > 0"), {"pid": waiting_pid}
            ):
                if second.done():
                    pytest.fail("competing publication completed without waiting for the lock")
        assert not second.done()
        release_first.set()
        assert await asyncio.wait_for(first, timeout=5) == "published"
        assert await asyncio.wait_for(second, timeout=5) == "obsolete"
        summaries = await _rows(factory, sid)
        assert len(summaries) == 1
        assert await _selected(factory, sid) == "winner"
    finally:
        if event.contains(engine.sync_engine, "before_cursor_execute", before_cursor_execute):
            event.remove(engine.sync_engine, "before_cursor_execute", before_cursor_execute)
        for task in (first, second):
            if task is not None and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
