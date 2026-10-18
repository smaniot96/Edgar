"""Session broadcast channel (EDG-047): ordering, retention, presence, limits, replay and audiences.

Offline: the Redis store is fakeredis and the subscriber is a fake, so these tests need neither
Postgres nor a network. Route-level behaviour (the EDG-046 membership helpers, token mode) is
covered in tests/api/test_session_events_api.py.

Secrets used in fixtures are distinctive strings. A test asserts they reach only the subscriber
whose audience is allowed to see them, and never appear in any other frame.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import aclosing
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from fakeredis import FakeAsyncRedis

from apps.api.schemas.channel import EVENT_TYPES, ChannelActor
from apps.api.services import session_broadcast, session_channel, session_stream
from apps.api.services.session_broadcast import TurnBroadcast
from apps.api.services.session_channel import (
    SubscriptionLimitError,
    Viewer,
    audience_for,
    audience_visible,
)

SID = 7


@pytest_asyncio.fixture(autouse=True)
async def _empty_store(redis_client: FakeAsyncRedis):
    """Each test starts from an empty store and a non-draining process.

    fakeredis clients can share data across tests in one process. An earlier app lifespan's shutdown
    leaves the process-wide drain flag set, so it is cleared here as well.
    """
    session_stream.reset_drain()
    await redis_client.flushdb()
    yield
    session_stream.reset_drain()
PUBLIC_SECRET = "SECRET-PUBLIC-NARRATION-41c9"
PRIVATE_SECRET = "SECRET-INVENTORY-7f3a9e"
REST_SECRET = "SECRET-REST-HIT-DIE-90ab"
CAROL_SECRET = "SECRET-CAROL-INVENTORY-22de"
OWNER = Viewer(user_id=10, role="owner")
BOB = Viewer(user_id=20, role="player")
CAROL = Viewer(user_id=21, role="player")
SPECTATOR = Viewer(user_id=30, role="spectator")
BOB_ACTOR = ChannelActor(user_id=20, display_name="Bob", character_id=2, character_name="Bran")
CAROL_ACTOR = ChannelActor(user_id=21, display_name="Carol", character_id=3, character_name="Cass")


class FakeSubscriber:
    """Stands in for the route's DB-backed subscriber; ``current`` is what the next re-check returns."""

    def __init__(self, viewer: Viewer | None, user_id: int = 0, name: str = "anon") -> None:
        self.current = viewer
        self._identity = {"user_id": user_id, "display_name": name, "character_id": None, "character_name": None}

    async def viewer(self) -> Viewer | None:
        return self.current

    async def identity(self) -> dict[str, Any]:
        return dict(self._identity)

    async def snapshot(self, head: int, viewer: Viewer) -> dict[str, Any]:
        return {"head_seq": head, "viewer_role": viewer.role}


def _parse(frame: str) -> dict[str, Any]:
    """One SSE frame as a dict: event name, id, and decoded JSON data (None for comments)."""
    out: dict[str, Any] = {"event": None, "id": None, "data": None}
    for line in frame.splitlines():
        if line.startswith("event: "):
            out["event"] = line[len("event: ") :]
        elif line.startswith("id: "):
            out["id"] = int(line[len("id: ") :])
        elif line.startswith("data: "):
            out["data"] = json.loads(line[len("data: ") :])
    return out


def _types(frames: list[str]) -> list[str]:
    """The table-channel event types delivered, in order. Presence, snapshots and private records are left out."""
    out: list[str] = []
    for frame in frames:
        data = _parse(frame)["data"]
        if data is None or data.get("type") is None:
            continue
        kind = data["type"]
        if kind == "snapshot" or kind.startswith("presence.") or EVENT_TYPES[kind][0] == "private":
            continue
        out.append(kind)
    return out


class Collector:
    """Runs one subscription in the background and lets a test wait for what it has received."""

    def __init__(self, gen) -> None:
        self.frames: list[str] = []
        self._gen = gen
        self._changed = asyncio.Condition()
        self.task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        async with aclosing(self._gen) as gen:
            async for frame in gen:
                self.frames.append(frame)
                async with self._changed:
                    self._changed.notify_all()

    async def until(self, predicate, timeout: float = 5.0) -> None:
        async def wait() -> None:
            async with self._changed:
                await self._changed.wait_for(lambda: predicate(self.frames) or self.task.done())

        await asyncio.wait_for(wait(), timeout)
        if self.task.done() and not predicate(self.frames):
            # The subscription ended (or failed) before the expected frames arrived: surface why.
            if not self.task.cancelled() and self.task.exception() is not None:
                raise self.task.exception()
            raise AssertionError(f"stream ended early; frames so far: {self.frames!r}")

    async def stop(self) -> None:
        """End the subscription cooperatively. Cancelling a blocked read would leave fakeredis desynchronised."""
        session_stream.begin_drain()
        try:
            await asyncio.wait_for(self.task, 3.0)
        except (TimeoutError, asyncio.CancelledError, Exception):
            self.task.cancel()
        finally:
            session_stream.reset_drain()


def _stream(redis, subscriber: FakeSubscriber, viewer: Viewer, last_event_id: int | None = None, conn: str | None = None):
    return session_stream.stream_frames(
        redis=redis,
        session_id=SID,
        subscriber=subscriber,
        viewer=viewer,
        last_event_id=last_event_id,
        conn_id=conn or f"conn-{id(subscriber)}-{last_event_id}",
    )


def _done(character_name: str, *, with_roll: bool = True, event_id: int = 1, secret: str = PRIVATE_SECRET) -> dict[str, Any]:
    """The `done` payload the turn runner commits, with secrets in the character and the rest evidence."""
    roll = (
        {
            "turn_id": "t",
            "check": "perception",
            "success": True,
            "dice_result": 17,
            "rest": {"hit_die": 8, "note": REST_SECRET},
        }
        if with_roll
        else None
    )
    return {
        "event_id": event_id,
        "narration": PUBLIC_SECRET,
        "citations": [{"reference_id": "r1"}],
        "ruling_citation_ids": ["r1"],
        "rules_edition": "2014",
        "character": {"name": character_name, "inventory": {"secret": secret}},
        "current_scene_id": "scene-1",
        "combat_state": None,
        "campaign_complete": False,
        "roll_status": "captured" if with_roll else "no_roll",
        "roll": roll,
    }


async def _commit_turn(redis, actor: ChannelActor, turn_id: str, done: dict[str, Any]) -> None:
    broadcast = TurnBroadcast(redis, SID, actor=actor, turn_id=turn_id)
    await broadcast.start(f"{actor.display_name} acts")
    await broadcast.observe('event: status\ndata: {"stage": "adjudicating"}\n\n')
    await broadcast.committed(done)


# --- Registry, audience and the single delivery rule ---------------------------------------------


def test_every_registered_event_has_a_channel_and_an_audience_kind() -> None:
    for event_type, (channel, kind) in EVENT_TYPES.items():
        assert channel in {"table", "narration", "rolls", "system", "private"}, event_type
        assert kind in {"table", "host", "player", "control"}, event_type


def test_unregistered_event_cannot_be_published() -> None:
    with pytest.raises(ValueError):
        audience_for("debug.frame", None)


@pytest.mark.asyncio
async def test_publish_refuses_unregistered_event_type(redis_client) -> None:
    with pytest.raises(ValueError):
        await session_channel.publish(
            redis_client, SID, kind="adjudication", actor=None, turn_id=None, data={}
        )


@pytest.mark.parametrize(
    ("audience", "viewer", "expected"),
    [
        ("table", OWNER, True),
        ("table", BOB, True),
        ("table", SPECTATOR, True),
        ("host", OWNER, True),
        ("host", BOB, False),
        ("host", SPECTATOR, False),
        ("player:20", BOB, True),
        ("player:20", OWNER, False),
        ("player:20", CAROL, False),
        ("player:20", SPECTATOR, False),
        ("player:30", SPECTATOR, False),
        ("control", OWNER, False),
        ("control", BOB, False),
        ("unknown:thing", OWNER, False),
        ("player:not-a-number", BOB, False),
    ],
)
def test_audience_visibility_matrix(audience: str, viewer: Viewer, expected: bool) -> None:
    assert audience_visible(audience, viewer) is expected


def test_private_events_are_addressed_to_the_actor_or_the_host_without_an_account() -> None:
    assert audience_for("character.state", 20) == "player:20"
    assert audience_for("turn.failed", 20) == "player:20"
    assert audience_for("character.state", None) == "host"
    assert audience_for("narration.committed", 20) == "table"


# --- Sequencing and retention -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_publishes_get_contiguous_sequence_numbers_in_log_order(redis_client) -> None:
    seqs = await asyncio.gather(
        *(
            session_channel.publish(
                redis_client, SID, kind="turn.action", actor=None, turn_id=None, data={"n": n}
            )
            for n in range(25)
        )
    )
    assert sorted(seqs) == list(range(1, 26))
    records = await session_channel.read_after(redis_client, SID, 0, count=100, block_ms=1)
    assert [seq for seq, _ in records] == list(range(1, 26))
    assert await session_channel.head_seq(redis_client, SID) == 25


@pytest.mark.asyncio
async def test_retention_trims_exactly_and_reports_the_oldest_retained_sequence(
    redis_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(session_channel, "RETENTION", 3)
    for n in range(6):
        await session_channel.publish(
            redis_client, SID, kind="turn.action", actor=None, turn_id=None, data={"n": n}
        )
    assert await session_channel.oldest_seq(redis_client, SID) == 4
    assert [seq for seq, _ in await session_channel.read_after(redis_client, SID, 3, block_ms=1)] == [4, 5, 6]


@pytest.mark.asyncio
async def test_envelope_carries_actor_and_turn_but_no_audience(redis_client) -> None:
    await session_channel.publish(
        redis_client, SID, kind="narration.committed", actor=BOB_ACTOR, turn_id="turn-9", data={"x": 1}
    )
    [(seq, record)] = await session_channel.read_after(redis_client, SID, 0, block_ms=1)
    assert seq == 1 and record["audience"] == "table"
    envelope = record["envelope"]
    assert envelope["channel"] == "narration"
    assert envelope["actor"]["display_name"] == "Bob"
    assert envelope["turn_id"] == "turn-9"
    assert "audience" not in envelope


# --- Turn broadcast: what reaches whom ----------------------------------------------------------


@pytest.mark.asyncio
async def test_committed_turn_splits_public_and_private_data_by_audience(redis_client) -> None:
    await _commit_turn(redis_client, BOB_ACTOR, "turn-b1", _done("Bran"))
    records = [rec for _seq, rec in await session_channel.read_after(redis_client, SID, 0, block_ms=1)]
    by_type = {rec["envelope"]["type"]: rec for rec in records}

    assert by_type["narration.committed"]["audience"] == "table"
    assert by_type["rolls.committed"]["audience"] == "table"
    assert by_type["state.committed"]["audience"] == "table"
    assert by_type["character.state"]["audience"] == f"player:{BOB_ACTOR.user_id}"

    table_text = json.dumps([rec for rec in records if rec["audience"] == "table"])
    assert PRIVATE_SECRET not in table_text
    assert REST_SECRET not in table_text
    assert PUBLIC_SECRET in table_text
    private_text = json.dumps(by_type["character.state"])
    assert PRIVATE_SECRET in private_text and REST_SECRET in private_text
    # The table's roll shows the outcome, never the private rest evidence.
    assert "rest" not in by_type["rolls.committed"]["envelope"]["data"]["roll"]


@pytest.mark.asyncio
async def test_failed_turn_tells_the_table_the_outcome_and_the_actor_the_safe_error(redis_client) -> None:
    broadcast = TurnBroadcast(redis_client, SID, actor=BOB_ACTOR, turn_id="turn-f")
    await broadcast.start("I attack")
    await broadcast.observe(
        'event: error\ndata: {"code": "turn_save_failed", "message": "Try again.", "outcome": "not_committed"}\n\n'
    )
    records = [rec for _seq, rec in await session_channel.read_after(redis_client, SID, 0, block_ms=1)]
    failed = [r for r in records if r["envelope"]["type"] == "turn.failed"]
    finished = [r for r in records if r["envelope"]["type"] == "turn.finished"]
    assert failed[0]["audience"] == f"player:{BOB_ACTOR.user_id}"
    assert finished[0]["audience"] == "table"
    assert finished[0]["envelope"]["data"]["outcome"] == "not_committed"


@pytest.mark.asyncio
async def test_unknown_outcome_is_published_as_unknown_and_closes_once(redis_client) -> None:
    broadcast = TurnBroadcast(redis_client, SID, actor=BOB_ACTOR, turn_id="turn-u")
    await broadcast.start("I wait")
    await broadcast.finish("unknown")
    await broadcast.finish("committed")  # already closed: no second outcome
    records = [rec for _seq, rec in await session_channel.read_after(redis_client, SID, 0, block_ms=1)]
    finished = [r["envelope"]["data"]["outcome"] for r in records if r["envelope"]["type"] == "turn.finished"]
    assert finished == ["unknown"]


@pytest.mark.asyncio
async def test_frames_outside_the_allowlist_are_never_published(redis_client) -> None:
    broadcast = TurnBroadcast(redis_client, SID, actor=BOB_ACTOR, turn_id="turn-t")
    await broadcast.start("I look")
    for chunk in (
        'event: token\ndata: {"text": "partial ' + PRIVATE_SECRET + '"}\n\n',
        'event: adjudication\ndata: {"reasoning": "' + PRIVATE_SECRET + '"}\n\n',
        'event: debug\ndata: {"dump": "' + PRIVATE_SECRET + '"}\n\n',
    ):
        await broadcast.observe(chunk)
    records = [rec for _seq, rec in await session_channel.read_after(redis_client, SID, 0, block_ms=1)]
    assert [r["envelope"]["type"] for r in records] == ["turn.action"]
    assert PRIVATE_SECRET not in json.dumps(records)


@pytest.mark.asyncio
async def test_a_publish_failure_never_reaches_the_turn(redis_client, monkeypatch: pytest.MonkeyPatch) -> None:
    async def broken(*_args, **_kwargs):
        raise ConnectionError("redis is down")

    monkeypatch.setattr(session_channel, "publish", broken)
    broadcast = TurnBroadcast(redis_client, SID, actor=BOB_ACTOR, turn_id="turn-x")
    await broadcast.start("I try")
    await broadcast.committed(_done("Bran"))  # must return normally
    await broadcast.finish("unknown")


@pytest.mark.asyncio
async def test_access_change_is_a_control_record_no_client_receives(redis_client) -> None:
    await session_broadcast.notify_access_changed(redis_client, [SID])
    [(seq, record)] = await session_channel.read_after(redis_client, SID, 0, block_ms=1)
    assert record["audience"] == "control"
    for viewer in (OWNER, BOB, SPECTATOR):
        assert not audience_visible(record["audience"], viewer)


# --- Presence: join, leave, heartbeat, TTL expiry --------------------------------------------------


def _identity(user_id: int, name: str, character: str | None = None) -> dict[str, Any]:
    return {"user_id": user_id, "display_name": name, "character_id": None, "character_name": character}


async def _types_in_log(redis) -> list[str]:
    return [rec["envelope"]["type"] for _s, rec in await session_channel.read_after(redis, SID, 0, block_ms=1)]


@pytest.mark.asyncio
async def test_presence_joins_once_per_member_and_leaves_with_the_last_connection(redis_client) -> None:
    bob = _identity(20, "Bob", "Bran")
    await session_channel.connect(redis_client, SID, "b-1", bob, now=1000.0)
    await session_channel.connect(redis_client, SID, "b-2", bob, now=1001.0)
    assert await _types_in_log(redis_client) == ["presence.joined"]

    roster = await session_channel.roster(redis_client, SID, now=1002.0)
    assert [(e.user_id, e.display_name, e.character_name, e.state) for e in roster] == [
        (20, "Bob", "Bran", "active")
    ]

    await session_channel.disconnect(redis_client, SID, "b-1", bob, now=1003.0)
    assert await _types_in_log(redis_client) == ["presence.joined"]
    await session_channel.disconnect(redis_client, SID, "b-2", bob, now=1004.0)
    assert await _types_in_log(redis_client) == ["presence.joined", "presence.left"]
    assert await session_channel.roster(redis_client, SID, now=1005.0) == []


@pytest.mark.asyncio
async def test_a_connection_that_stops_heartbeating_expires_and_is_announced_left(redis_client) -> None:
    carol = _identity(21, "Carol", "Cass")
    await session_channel.connect(redis_client, SID, "c-1", carol, now=1000.0)
    ttl = session_channel.PRESENCE_TTL_SECONDS

    # Still alive just before expiry, then gone after it, without any disconnect call.
    assert [e.user_id for e in await session_channel.roster(redis_client, SID, now=1000.0 + ttl - 1)] == [21]
    assert await session_channel.roster(redis_client, SID, now=1000.0 + ttl + 1) == []
    assert await _types_in_log(redis_client) == ["presence.joined", "presence.left"]


@pytest.mark.asyncio
async def test_heartbeat_extends_presence_and_a_late_heartbeat_reregisters(redis_client) -> None:
    carol = _identity(21, "Carol")
    ttl = session_channel.PRESENCE_TTL_SECONDS
    await session_channel.connect(redis_client, SID, "c-1", carol, now=1000.0)
    await session_channel.heartbeat(redis_client, SID, "c-1", carol, now=1000.0 + ttl - 1)
    assert [e.user_id for e in await session_channel.roster(redis_client, SID, now=1000.0 + ttl + 5)] == [21]

    # After expiry a heartbeat registers the connection again and the member rejoins.
    late = 1000.0 + 10 * ttl
    await session_channel.heartbeat(redis_client, SID, "c-1", carol, now=late)
    assert [e.user_id for e in await session_channel.roster(redis_client, SID, now=late + 1)] == [21]
    assert await _types_in_log(redis_client) == ["presence.joined", "presence.left", "presence.joined"]


@pytest.mark.asyncio
async def test_idle_is_reported_once_per_change_and_only_for_present_members(redis_client) -> None:
    bob = _identity(20, "Bob")
    await session_channel.connect(redis_client, SID, "b-1", bob, now=1.0)
    assert await session_channel.set_state(redis_client, SID, 20, "idle", bob, now=2.0) is True
    assert await session_channel.set_state(redis_client, SID, 20, "idle", bob, now=3.0) is False
    assert await session_channel.set_state(redis_client, SID, 20, "active", bob, now=4.0) is True
    assert await _types_in_log(redis_client) == ["presence.joined", "presence.idle", "presence.active"]

    # An absent member's state is recorded for their next roster read, but nothing is announced.
    await session_channel.disconnect(redis_client, SID, "b-1", bob, now=5.0)
    await session_channel.set_state(redis_client, SID, 20, "idle", bob, now=6.0)
    assert await _types_in_log(redis_client) == [
        "presence.joined", "presence.idle", "presence.active", "presence.left"
    ]


@pytest.mark.asyncio
async def test_connection_limits_per_session_and_per_user(redis_client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session_channel, "MAX_CONNECTIONS_PER_USER_SESSION", 2)
    monkeypatch.setattr(session_channel, "MAX_SUBSCRIBERS_PER_SESSION", 3)
    bob = _identity(20, "Bob")
    for n in range(2):
        await session_channel.admit(redis_client, SID, 20, now=1.0)
        await session_channel.connect(redis_client, SID, f"b-{n}", bob, now=1.0)
    with pytest.raises(SubscriptionLimitError) as own:
        await session_channel.admit(redis_client, SID, 20, now=1.0)
    assert own.value.code == "user_connections_full"

    await session_channel.connect(redis_client, SID, "c-1", _identity(21, "Carol"), now=1.0)
    with pytest.raises(SubscriptionLimitError) as full:
        await session_channel.admit(redis_client, SID, 22, now=1.0)
    assert full.value.code == "session_subscribers_full"


@pytest.mark.asyncio
async def test_expired_connections_do_not_count_against_limits(redis_client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session_channel, "MAX_CONNECTIONS_PER_USER_SESSION", 1)
    await session_channel.connect(redis_client, SID, "b-old", _identity(20, "Bob"), now=1.0)
    later = 1.0 + session_channel.PRESENCE_TTL_SECONDS + 1
    await session_channel.admit(redis_client, SID, 20, now=later)  # no raise: the old one has expired


@pytest.mark.asyncio
async def test_stream_slots_are_bounded_and_release_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session_stream, "MAX_STREAMS_PER_PROCESS", 2)
    before = session_stream.open_stream_count()
    first = session_stream.acquire_slot()
    second = session_stream.acquire_slot()
    assert first is not None and second is not None
    assert session_stream.acquire_slot() is None
    first.release()
    first.release()  # a second release must not free another slot
    assert session_stream.open_stream_count() == before + 1
    third = session_stream.acquire_slot()
    assert third is not None
    second.release()
    third.release()
    assert session_stream.open_stream_count() == before


# --- Subscriber loop: live order, replay, cut-off, bounds ----------------------------------------


@pytest.mark.asyncio
async def test_two_members_receive_each_others_turns_live_and_in_the_same_order(redis_client) -> None:
    owner = Collector(_stream(redis_client, FakeSubscriber(OWNER, 10, "Alice"), OWNER))
    bob = Collector(_stream(redis_client, FakeSubscriber(BOB, 20, "Bob"), BOB))
    carol = Collector(_stream(redis_client, FakeSubscriber(CAROL, 21, "Carol"), CAROL))
    spectator = Collector(_stream(redis_client, FakeSubscriber(SPECTATOR, 30, "Sam"), SPECTATOR))
    collectors = [owner, bob, carol, spectator]
    for c in collectors:
        await c.until(lambda frames: any("event: snapshot" in f for f in frames))

    await _commit_turn(redis_client, BOB_ACTOR, "turn-1", _done("Bran", event_id=1))
    await _commit_turn(redis_client, CAROL_ACTOR, "turn-2", _done("Cass", event_id=2))
    expected_last = await session_channel.head_seq(redis_client, SID)
    for c in collectors:
        await c.until(
            lambda frames: any(
                _parse(f)["id"] == expected_last and _parse(f)["event"] == "turn.finished" for f in frames
            )
        )

    table_order = [_types(c.frames) for c in collectors]
    assert table_order[0] == table_order[1] == table_order[2] == table_order[3]
    assert table_order[0].count("turn.finished") == 2
    for c in collectors:
        await c.stop()


@pytest.mark.asyncio
async def test_private_events_reach_only_the_seated_player_who_acted(redis_client) -> None:
    owner = Collector(_stream(redis_client, FakeSubscriber(OWNER, 10), OWNER))
    bob = Collector(_stream(redis_client, FakeSubscriber(BOB, 20), BOB))
    spectator = Collector(_stream(redis_client, FakeSubscriber(SPECTATOR, 30), SPECTATOR))
    carol = Collector(_stream(redis_client, FakeSubscriber(CAROL, 21), CAROL))
    collectors = [owner, bob, spectator, carol]
    for c in collectors:
        await c.until(lambda frames: any("event: snapshot" in f for f in frames))

    await _commit_turn(redis_client, BOB_ACTOR, "turn-p", _done("Bran"))
    head = await session_channel.head_seq(redis_client, SID)
    for c in collectors:
        await c.until(lambda frames: any(_parse(f)["id"] == head for f in frames))

    assert any("character.state" in f for f in bob.frames)
    for other in (owner, spectator, carol):
        assert not any("character.state" in f for f in other.frames)
        assert PRIVATE_SECRET not in "".join(other.frames)
        assert REST_SECRET not in "".join(other.frames)
    assert PRIVATE_SECRET in "".join(bob.frames)
    for c in collectors:
        await c.stop()


@pytest.mark.asyncio
async def test_spectator_receives_table_channels_and_no_player_or_host_records(redis_client) -> None:
    await session_channel.publish(
        redis_client, SID, kind="turn.failed", actor=BOB_ACTOR, turn_id="t", data={"message": PRIVATE_SECRET}
    )
    await session_channel.publish(
        redis_client, SID, kind="narration.committed", actor=BOB_ACTOR, turn_id="t", data={"narration": PUBLIC_SECRET}
    )
    frames = []
    async with aclosing(_stream(redis_client, FakeSubscriber(SPECTATOR, 30), SPECTATOR, last_event_id=0)) as gen:
        async for frame in gen:
            frames.append(frame)
            if any("narration.committed" in f for f in frames):
                break
    text = "".join(frames)
    assert PUBLIC_SECRET in text
    assert PRIVATE_SECRET not in text


@pytest.mark.asyncio
async def test_host_only_records_reach_the_owner_and_nobody_else(redis_client) -> None:
    # No registered event uses the host audience, so the rule is exercised on a record written the
    # way a future host-only event would be.
    record = {
        "audience": "host",
        "envelope": {"seq": 1, "type": "turn.action", "channel": "system", "actor": None, "turn_id": None,
                     "at": datetime.now(UTC).isoformat(), "data": {"note": PRIVATE_SECRET}},
    }
    await redis_client.xadd(f"edgar:session:{SID}:log", {"r": json.dumps(record)}, id="1-0")
    await redis_client.set(f"edgar:session:{SID}:seq", 1)

    async def take(viewer: Viewer, user_id: int) -> str:
        out: list[str] = []
        async with aclosing(_stream(redis_client, FakeSubscriber(viewer, user_id), viewer, last_event_id=0)) as gen:
            async for frame in gen:
                out.append(frame)
                if any(PRIVATE_SECRET in f for f in out) or len(out) >= 4:
                    break
        return "".join(out)

    assert PRIVATE_SECRET in await take(OWNER, 10)
    assert PRIVATE_SECRET not in await take(BOB, 20)
    assert PRIVATE_SECRET not in await take(SPECTATOR, 30)


@pytest.mark.asyncio
async def test_reconnect_replays_exactly_the_missed_records_the_viewer_may_see(redis_client) -> None:
    await _commit_turn(redis_client, BOB_ACTOR, "turn-r1", _done("Bran", event_id=1))
    bob_first = Collector(_stream(redis_client, FakeSubscriber(BOB, 20), BOB, last_event_id=0))
    await bob_first.until(lambda frames: any("turn.finished" in f for f in frames))
    last_seen = max(_parse(f)["id"] for f in bob_first.frames if _parse(f)["id"] is not None)
    await bob_first.stop()

    # While Bob is away: Carol's turn (her private record) and Bob's next turn.
    await _commit_turn(redis_client, CAROL_ACTOR, "turn-r2", _done("Cass", event_id=2, secret=CAROL_SECRET))
    await _commit_turn(redis_client, BOB_ACTOR, "turn-r3", _done("Bran", event_id=3))

    resumed = Collector(_stream(redis_client, FakeSubscriber(BOB, 20), BOB, last_event_id=last_seen))
    # The subscription publishes Bob's own presence join too, so the expectation is taken from the log
    # after the stream has caught up, not from the head read above.
    final_head = await session_channel.head_seq(redis_client, SID)
    log_records = await session_channel.read_after(redis_client, SID, 0, count=500, block_ms=1)
    expected = [seq for seq, rec in log_records if last_seen < seq <= final_head and audience_visible(rec["audience"], BOB)]

    def delivered_ids(frames: list[str]) -> list[int]:
        return [_parse(f)["id"] for f in frames if _parse(f)["id"] is not None]

    await resumed.until(lambda frames: set(expected) <= set(delivered_ids(frames)))
    await resumed.stop()
    # Records published by the close itself (Bob's own presence leave) fall after final_head.
    delivered = [i for i in delivered_ids(resumed.frames) if i <= final_head]
    assert delivered == expected
    assert not any(_parse(f)["event"] == "snapshot" for f in resumed.frames)
    # Who acted (and as which PC) is public; Carol's private projection and rest evidence are not.
    assert CAROL_SECRET not in "".join(resumed.frames)
    assert not any('"type":"character.state"' in f and "turn-r2" in f for f in resumed.frames)
    # Bob's own private record for his own turn is his to receive.
    assert any('"type":"character.state"' in f and "turn-r3" in f for f in resumed.frames)
    assert "turn-r3" in "".join(resumed.frames)


@pytest.mark.asyncio
async def test_a_client_behind_the_retained_window_gets_a_snapshot_not_a_gap(
    redis_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(session_channel, "RETENTION", 3)
    for n in range(6):
        await session_channel.publish(
            redis_client, SID, kind="turn.action", actor=BOB_ACTOR, turn_id=f"t{n}", data={"n": n}
        )
    frames: list[str] = []
    async with aclosing(_stream(redis_client, FakeSubscriber(OWNER, 10), OWNER, last_event_id=1)) as gen:
        async for frame in gen:
            frames.append(frame)
            if len(frames) >= 2:
                break
    parsed = [_parse(f) for f in frames]
    assert parsed[1]["event"] == "snapshot"
    # Six turn actions plus this viewer's own presence join, which is published before the snapshot.
    assert parsed[1]["id"] == 7


@pytest.mark.asyncio
async def test_a_new_subscriber_gets_a_snapshot_and_no_history(redis_client) -> None:
    await _commit_turn(redis_client, BOB_ACTOR, "turn-old", _done("Bran"))
    frames: list[str] = []
    async with aclosing(_stream(redis_client, FakeSubscriber(OWNER, 10), OWNER)) as gen:
        async for frame in gen:
            frames.append(frame)
            if len(frames) >= 2:
                break
    assert _parse(frames[1])["event"] == "snapshot"
    assert not any(_parse(f)["event"] == "narration.committed" for f in frames)


@pytest.mark.asyncio
async def test_a_removed_member_is_cut_off_at_once_and_receives_nothing_after(redis_client) -> None:
    subscriber = FakeSubscriber(BOB, 20, "Bob")
    bob = Collector(_stream(redis_client, subscriber, BOB))
    await bob.until(lambda frames: any("snapshot" in f for f in frames))

    subscriber.current = None  # the membership is gone
    await session_broadcast.notify_access_changed(redis_client, [SID])
    await _commit_turn(redis_client, CAROL_ACTOR, "turn-after", _done("Cass"))
    await bob.until(lambda frames: any(_parse(f)["event"] == "closed" for f in frames), timeout=2.0)

    closed_index = next(i for i, f in enumerate(bob.frames) if _parse(f)["event"] == "closed")
    assert _parse(bob.frames[closed_index])["data"] == {"reason": "access_revoked"}
    assert len(bob.frames) == closed_index + 1
    assert not any("narration" in f for f in bob.frames)
    await bob.stop()


@pytest.mark.asyncio
async def test_slow_consumer_is_closed_and_can_resume_from_its_cursor(
    redis_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(session_stream, "SLOW_CONSUMER_SECONDS", 0.05)
    for n in range(3):
        await session_channel.publish(
            redis_client, SID, kind="turn.action", actor=None, turn_id=None, data={"n": n}
        )
    gen = _stream(redis_client, FakeSubscriber(OWNER, 10), OWNER, last_event_id=0)
    async with aclosing(gen) as frames:
        first = await frames.__anext__()
        while _parse(first)["event"] in (None, "retry"):
            first = await frames.__anext__()
        assert _parse(first)["id"] == 1
        await asyncio.sleep(0.2)  # the client drains slower than the threshold
        second = await frames.__anext__()
    assert _parse(second)["event"] == "closed"
    assert _parse(second)["data"] == {"reason": "slow_consumer"}


@pytest.mark.asyncio
async def test_server_shutdown_ends_open_streams_with_a_reason(redis_client) -> None:
    collector = Collector(_stream(redis_client, FakeSubscriber(OWNER, 10), OWNER))
    await collector.until(lambda frames: any("snapshot" in f for f in frames))
    session_stream.begin_drain()
    try:
        await collector.until(lambda frames: any(_parse(f)["event"] == "closed" for f in frames), timeout=2.0)
        closed = next(f for f in collector.frames if _parse(f)["event"] == "closed")
        assert _parse(closed)["data"] == {"reason": "server_shutdown"}
    finally:
        session_stream.reset_drain()
        await collector.stop()


@pytest.mark.asyncio
async def test_lifetime_cap_closes_the_stream_so_the_client_reconnects(
    redis_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(session_stream, "MAX_STREAM_SECONDS", 0.05)
    monkeypatch.setattr(session_channel, "READ_BLOCK_MS", 20)
    frames = []
    async with aclosing(_stream(redis_client, FakeSubscriber(OWNER, 10), OWNER)) as gen:
        async for frame in gen:
            frames.append(frame)
    assert _parse(frames[-1])["data"] == {"reason": "lifetime"}


@pytest.mark.asyncio
async def test_idle_streams_send_heartbeat_comments(redis_client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session_stream, "HEARTBEAT_SECONDS", 0.02)
    monkeypatch.setattr(session_channel, "READ_BLOCK_MS", 10)
    frames = []
    async with aclosing(_stream(redis_client, FakeSubscriber(OWNER, 10), OWNER)) as gen:
        async for frame in gen:
            frames.append(frame)
            if sum(1 for f in frames if f.startswith(": ping")) >= 2:
                break
    assert sum(1 for f in frames if f.startswith(": ping")) >= 2


@pytest.mark.asyncio
async def test_stream_frames_never_carry_tokens_or_audiences(redis_client) -> None:
    await _commit_turn(redis_client, BOB_ACTOR, "turn-clean", _done("Bran"))
    frames = []
    async with aclosing(_stream(redis_client, FakeSubscriber(BOB, 20), BOB, last_event_id=0)) as gen:
        async for frame in gen:
            frames.append(frame)
            if any("turn.finished" in f for f in frames):
                break
    text = "".join(frames)
    assert "audience" not in text
    assert "Bearer" not in text and "edgar_" not in text
