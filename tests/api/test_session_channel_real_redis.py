"""Session channel against a REAL Redis (EDG-047). Skipped unless EDGAR_TEST_REDIS_URL is set.

fakeredis cannot prove the cross-process ordering guarantee, so these flows run two or more OS
processes, each with its own Redis client, against one session.

Run against a throwaway server (never a developer's own stack):

    docker run -d --rm --name edg047-redis -p 56379:6379 redis:7
    EDGAR_TEST_REDIS_URL=redis://localhost:56379/0 uv run pytest tests/api/test_session_channel_real_redis.py
    docker rm -f edg047-redis

The commands used need Redis 5.0 or later (XADD with explicit IDs and MAXLEN, XREAD COUNT/BLOCK,
XRANGE COUNT, WATCH/MULTI). The fixture asserts the server version.
"""

from __future__ import annotations

import asyncio
import json
import multiprocessing as mp
import os
import random
from contextlib import aclosing

import pytest
import pytest_asyncio
from redis.asyncio import Redis

from apps.api.services import session_channel, session_stream
from tests.api.test_session_channel import OWNER, FakeSubscriber, _parse

URL = os.environ.get("EDGAR_TEST_REDIS_URL")
pytestmark = pytest.mark.skipif(
    not URL, reason="EDGAR_TEST_REDIS_URL not set: real-Redis checks are opt-in"
)

# Two publisher processes by default (200 publishes). EDGAR_TEST_REDIS_PROCESSES raises contention.
PROCESSES = int(os.environ.get("EDGAR_TEST_REDIS_PROCESSES", "2"))
PER_PROCESS = 200 // PROCESSES
PUBLISH_GAP_SECONDS = 0.002  # keeps publishers running long enough for a mid-run cut


def _publisher_main(url: str, sid: int, tag: str, count: int, gap: float) -> None:
    """A separate OS process with its own Redis client, publishing ``count`` events to one session."""

    async def run() -> None:
        client = Redis.from_url(url, decode_responses=True)
        try:
            for i in range(count):
                await session_channel.publish(
                    client, sid, kind="turn.action", actor=None, turn_id=None, data={"p": tag, "i": i}
                )
                if gap:
                    await asyncio.sleep(gap)
        finally:
            await client.aclose()

    asyncio.run(run())


def _presence_child_main(url: str, sid: int, uid: int, ttl: float) -> None:
    """A worker that holds a presence connection and heartbeats until it is killed."""

    async def run() -> None:
        session_channel.PRESENCE_TTL_SECONDS = ttl
        client = Redis.from_url(url, decode_responses=True)
        identity = {"user_id": uid, "display_name": "Killed", "character_id": None, "character_name": None}
        await session_channel.connect(client, sid, f"conn-{uid}", identity)
        while True:
            await asyncio.sleep(0.3)
            await session_channel.heartbeat(client, sid, f"conn-{uid}", identity)

    asyncio.run(run())


def _spawn(target, *args) -> mp.process.BaseProcess:
    # "spawn" gives every process a clean interpreter and its own event loop and client.
    proc = mp.get_context("spawn").Process(target=target, args=args)
    proc.start()
    return proc


async def _join(procs: list) -> None:
    for proc in procs:
        await asyncio.to_thread(proc.join, 180)
        assert proc.exitcode == 0, f"publisher exited with {proc.exitcode}"


async def _wait_for(predicate, timeout: float = 30.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.02)


def _ids(frames: list[str]) -> list[int]:
    """Sequence numbers of delivered records (a snapshot's id is its head, not a record)."""
    return [
        _parse(f)["id"]
        for f in frames
        if _parse(f)["id"] is not None and _parse(f)["event"] != "snapshot"
    ]


@pytest_asyncio.fixture
async def real():
    """A real Redis client and a fresh session id. Keys for the session are removed afterwards."""
    client = Redis.from_url(URL, decode_responses=True)
    version = tuple(int(part) for part in (await client.info("server"))["redis_version"].split(".")[:2])
    assert version >= (5, 0), f"Redis {version} is older than the required 5.0"
    sid = random.randint(10**6, 10**9)
    session_stream.reset_drain()
    yield client, sid
    session_stream.reset_drain()
    keys = [key async for key in client.scan_iter(f"edgar:session:{sid}:*")]
    if keys:
        await client.delete(*keys)
    await client.aclose()


def _log_key(sid: int) -> str:
    return f"edgar:session:{sid}:log"


def _seq_key(sid: int) -> str:
    return f"edgar:session:{sid}:seq"


@pytest.mark.asyncio
async def test_two_processes_publish_200_events_in_contiguous_order(real) -> None:
    client, sid = real
    procs = [_spawn(_publisher_main, URL, sid, f"p{k}", PER_PROCESS, PUBLISH_GAP_SECONDS) for k in range(PROCESSES)]
    await _join(procs)

    entries = await client.xrange(_log_key(sid), min="-", max="+", count=10_000)
    stream_ids = [int(entry_id.split("-", 1)[0]) for entry_id, _fields in entries]
    envelopes = [json.loads(fields["r"])["envelope"] for _entry_id, fields in entries]
    total = PROCESSES * PER_PROCESS

    assert len(entries) == total
    assert stream_ids == list(range(1, total + 1)), "stream order is not the contiguous sequence"
    assert [env["seq"] for env in envelopes] == stream_ids, "envelope seq disagrees with stream order"
    assert len({env["seq"] for env in envelopes}) == total, "duplicate sequence numbers"
    assert int(await client.get(_seq_key(sid))) == total
    for k in range(PROCESSES):
        own = [env["data"]["i"] for env in envelopes if env["data"]["p"] == f"p{k}"]
        assert own == list(range(PER_PROCESS)), "a publisher's own events were reordered"


@pytest.mark.asyncio
async def test_live_subscriber_gets_every_event_exactly_once_in_order(real) -> None:
    client, sid = real
    frames: list[str] = []

    async def consume(gen) -> None:
        async with aclosing(gen) as stream:
            async for frame in stream:
                frames.append(frame)

    gen = session_stream.stream_frames(
        redis=client,
        session_id=sid,
        subscriber=FakeSubscriber(OWNER, 10, "Alice"),
        viewer=OWNER,
        last_event_id=None,
        conn_id="live-real",
    )
    consumer = asyncio.create_task(consume(gen))
    await _wait_for(lambda: any("event: snapshot" in f for f in frames))
    snapshot_head = next(_parse(f)["id"] for f in frames if "event: snapshot" in f)

    procs = [_spawn(_publisher_main, URL, sid, f"p{k}", PER_PROCESS, PUBLISH_GAP_SECONDS) for k in range(PROCESSES)]
    await _join(procs)
    head = int(await client.get(_seq_key(sid)))
    await _wait_for(lambda: head in _ids(frames), timeout=30)
    session_stream.begin_drain()
    try:
        await asyncio.wait_for(consumer, 10)
    finally:
        session_stream.reset_drain()

    delivered = _ids(frames)
    assert delivered == sorted(set(delivered)), "duplicate or out-of-order frames"
    assert delivered == list(range(snapshot_head + 1, head + 1)), "a live event was missed"


@pytest.mark.asyncio
async def test_resume_after_a_cut_mid_run_replays_exactly_the_missed_events(real) -> None:
    client, sid = real
    first: list[str] = []
    procs = [_spawn(_publisher_main, URL, sid, f"p{k}", PER_PROCESS, PUBLISH_GAP_SECONDS) for k in range(PROCESSES)]

    gen = session_stream.stream_frames(
        redis=client,
        session_id=sid,
        subscriber=FakeSubscriber(OWNER, 10, "Alice"),
        viewer=OWNER,
        last_event_id=None,
        conn_id="cut-real-1",
    )
    async with aclosing(gen) as stream:
        async for frame in stream:
            first.append(frame)
            if len(_ids(first)) >= 60:
                break  # the cut: the client drops mid-run, publishers still running
    last_seen = max(_ids(first))

    await _join(procs)
    head = int(await client.get(_seq_key(sid)))
    assert head > last_seen, "the cut must leave events to replay"

    resumed: list[str] = []
    gen2 = session_stream.stream_frames(
        redis=client,
        session_id=sid,
        subscriber=FakeSubscriber(OWNER, 10, "Alice"),
        viewer=OWNER,
        last_event_id=last_seen,
        conn_id="cut-real-2",
    )
    async with aclosing(gen2) as stream:
        # The first frame is sent after this subscriber has registered, so its own presence join is
        # already in the log: the target is read from the log at that point.
        async for frame in stream:
            resumed.append(frame)
            if len(resumed) == 1:
                target = int(await client.get(_seq_key(sid)))
            if len(resumed) > 1 and target in _ids(resumed):
                break

    records = await session_channel.read_after(client, sid, 0, count=10_000, block_ms=1)
    expected = [seq for seq, rec in records if last_seen < seq <= target and rec["audience"] == "table"]
    delivered = _ids(resumed)
    assert delivered == expected, "replay after the cut is not exactly the missed events"
    assert len(delivered) == len(set(delivered))
    assert not any(_parse(f)["event"] == "snapshot" for f in resumed), "a valid cursor must not snapshot"


@pytest.mark.asyncio
async def test_trimming_past_the_window_forces_a_snapshot(real, monkeypatch: pytest.MonkeyPatch) -> None:
    client, sid = real
    monkeypatch.setattr(session_channel, "RETENTION", 20)
    for i in range(50):
        await session_channel.publish(client, sid, kind="turn.action", actor=None, turn_id=None, data={"i": i})

    assert int(await client.xlen(_log_key(sid))) == 20, "MAXLEN must trim exactly"
    assert await session_channel.oldest_seq(client, sid) == 31
    frames: list[str] = []
    gen = session_stream.stream_frames(
        redis=client,
        session_id=sid,
        subscriber=FakeSubscriber(OWNER, 10, "Alice"),
        viewer=OWNER,
        last_event_id=5,
        conn_id="trim-real",
    )
    async with aclosing(gen) as stream:
        async for frame in stream:
            frames.append(frame)
            if any("event: snapshot" in f for f in frames):
                break
    snapshot = next(_parse(f) for f in frames if "event: snapshot" in f)
    # 50 publishes plus this subscriber's own presence join, which is logged before the snapshot.
    assert snapshot["id"] == 51
    assert not any(_parse(f)["id"] is not None and _parse(f)["event"] != "snapshot" for f in frames)


@pytest.mark.asyncio
async def test_a_killed_worker_is_removed_by_presence_expiry(real) -> None:
    client, sid = real
    uid = 4242
    ttl = 1.0
    worker = _spawn(_presence_child_main, URL, sid, uid, ttl)
    await _until(lambda: _is_present(client, sid, uid))
    # Heartbeats must keep the member present for longer than one TTL.
    await asyncio.sleep(ttl * 2.5)
    assert await _is_present(client, sid, uid), "heartbeats must keep the member present"

    worker.kill()
    await asyncio.to_thread(worker.join, 30)
    await asyncio.sleep(ttl + 1.0)
    roster_after = await session_channel.roster(client, sid)
    assert not any(entry.user_id == uid for entry in roster_after), "a killed worker's member must expire"
    records = await session_channel.read_after(client, sid, 0, count=10_000, block_ms=1)
    left = [rec["envelope"]["data"]["user_id"] for _seq, rec in records if rec["envelope"]["type"] == "presence.left"]
    assert uid in left


async def _is_present(client: Redis, sid: int, uid: int) -> bool:
    return any(entry.user_id == uid for entry in await session_channel.roster(client, sid))


async def _until(predicate, timeout: float = 30.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not await predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.05)
