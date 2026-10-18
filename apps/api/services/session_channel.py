"""The per-session broadcast log, presence and admission limits, kept in Redis (EDG-047).

Storage, per session ``{id}``:

* ``edgar:session:{id}:seq``   integer, the last sequence number issued (never expires).
* ``edgar:session:{id}:log``   a Redis Stream whose entry ids are ``{seq}-0``. It holds the last
  ``RETENTION`` events, trimmed exactly (``MAXLEN``), so entries are contiguous in ``seq``.
* ``edgar:session:{id}:live``  sorted set of live connection ids scored by expiry (epoch seconds).
* ``edgar:session:{id}:conns`` hash connection id -> presence identity (display name, PC, user id).
* ``edgar:session:{id}:state`` hash user id -> ``active`` | ``idle`` (client-reported attention).

A publish takes ``seq`` and appends to the log in one optimistic transaction (WATCH on ``seq``), so
the log's order is the sequence order even when several workers publish. Replay and live delivery
are the same operation: read the log after a cursor. A client that is behind the retained window
is detected from the log's first entry and gets a snapshot instead.

Presence is connection-based: a member is present while they hold at least one live connection,
and each connection's expiry is refreshed by its heartbeat. A connection whose worker died expires
after ``PRESENCE_TTL_SECONDS``; the sweep then announces ``presence.left``. Clocks are compared as
wall-clock epoch seconds, which assumes the workers' clocks agree to within seconds.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog
from redis.asyncio import Redis
from redis.exceptions import WatchError

from apps.api.schemas.channel import (
    AUDIENCE_CONTROL,
    AUDIENCE_HOST,
    AUDIENCE_PLAYER_PREFIX,
    AUDIENCE_TABLE,
    EVENT_TYPES,
    ChannelActor,
    ChannelEnvelope,
    PresenceEntry,
    PresenceState,
)
from apps.api.services.safe_errors import classify_error, log_failure

log = structlog.get_logger()

# Replay window: the last events kept per session. A client further behind than this gets a snapshot.
RETENTION = 500
STREAM_TTL_SECONDS = 30 * 24 * 3600
PRESENCE_TTL_SECONDS = 30.0
HEARTBEAT_SECONDS = 15.0
RECHECK_SECONDS = 5.0
# Longest a subscriber waits on an empty log before it re-checks presence and shutdown.
READ_BLOCK_MS = 1000
READ_BATCH = 100
SLOW_CONSUMER_SECONDS = 10.0
MAX_STREAM_SECONDS = 1800.0
MAX_STREAMS_PER_PROCESS = 200
MAX_SUBSCRIBERS_PER_SESSION = 32
MAX_CONNECTIONS_PER_USER_SESSION = 3
_MAX_PUBLISH_ATTEMPTS = 50

OWNER_ROLE = "owner"
PLAYER_ROLE = "player"
SPECTATOR_ROLE = "spectator"


class SubscriptionLimitError(Exception):
    """A connection limit was reached. ``code`` is a stable, value-free reason."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Viewer:
    """Who is reading the channel right now, as re-resolved from the membership rows."""

    user_id: int | None
    role: str


def audience_visible(audience: str, viewer: Viewer) -> bool:
    """The single delivery rule, used for live events and for replay alike. Unknown audiences are denied."""
    if audience == AUDIENCE_TABLE:
        return True
    if audience == AUDIENCE_HOST:
        return viewer.role == OWNER_ROLE
    if audience == AUDIENCE_CONTROL:
        return False
    if audience.startswith(AUDIENCE_PLAYER_PREFIX):
        try:
            target = int(audience[len(AUDIENCE_PLAYER_PREFIX) :])
        except ValueError:
            return False
        return viewer.role in (OWNER_ROLE, PLAYER_ROLE) and viewer.user_id == target
    return False


def audience_for(event_type: str, actor_user_id: int | None) -> str:
    """Resolve a registered event's audience. Player-private events need an account to address."""
    if event_type not in EVENT_TYPES:
        raise ValueError(f"unregistered channel event: {event_type}")
    _channel, kind = EVENT_TYPES[event_type]
    if kind == "player":
        return AUDIENCE_HOST if actor_user_id is None else f"{AUDIENCE_PLAYER_PREFIX}{actor_user_id}"
    return kind


def _key(session_id: int, name: str) -> str:
    return f"edgar:session:{session_id}:{name}"


def _now(now: float | None) -> float:
    return time.time() if now is None else now


async def head_seq(redis: Redis, session_id: int) -> int:
    """The last sequence number issued for the session (0 when none)."""
    return int(await redis.get(_key(session_id, "seq")) or 0)


async def oldest_seq(redis: Redis, session_id: int) -> int | None:
    """The first sequence number still retained in the log, or None when the log is empty."""
    rows = await redis.xrange(_key(session_id, "log"), min="-", max="+", count=1)
    if not rows:
        return None
    return int(rows[0][0].split("-", 1)[0])


async def publish(
    redis: Redis,
    session_id: int,
    *,
    kind: str,
    actor: ChannelActor | None,
    turn_id: str | None,
    data: dict[str, Any],
    now: datetime | None = None,
) -> int:
    """Append one registered event (``kind`` is its type) and return its sequence number.

    The audience is derived here from the registry and the actor, never taken from the caller.
    """
    event_type = kind
    if event_type not in EVENT_TYPES:
        raise ValueError(f"unregistered channel event: {event_type}")
    channel, _kind = EVENT_TYPES[event_type]
    audience = audience_for(event_type, actor.user_id if actor is not None else None)
    seq_key = _key(session_id, "seq")
    log_key = _key(session_id, "log")
    stamped = now or datetime.now(UTC)
    for _attempt in range(_MAX_PUBLISH_ATTEMPTS):
        async with redis.pipeline(transaction=True) as pipe:
            try:
                await pipe.watch(seq_key)
                seq = int(await pipe.get(seq_key) or 0) + 1
                envelope = ChannelEnvelope(
                    seq=seq,
                    type=event_type,
                    channel=channel,
                    actor=actor,
                    turn_id=turn_id,
                    at=stamped,
                    data=data,
                )
                record = json.dumps(
                    {"audience": audience, "envelope": envelope.model_dump(mode="json")},
                    separators=(",", ":"),
                )
                pipe.multi()
                pipe.set(seq_key, seq)
                pipe.xadd(log_key, {"r": record}, id=f"{seq}-0", maxlen=RETENTION, approximate=False)
                pipe.expire(log_key, STREAM_TTL_SECONDS)
                await pipe.execute()
                return seq
            except WatchError:
                continue
    raise RuntimeError("session channel publish did not win the sequence race")


async def read_after(
    redis: Redis,
    session_id: int,
    cursor: int,
    *,
    count: int | None = None,
    block_ms: int | None = None,
) -> list[tuple[int, dict[str, Any]]]:
    """Retained records with sequence numbers greater than ``cursor``, oldest first.

    Blocks up to ``block_ms`` when there is nothing new. Each record is ``{"audience", "envelope"}``.
    """
    batch = READ_BATCH if count is None else count
    wait = READ_BLOCK_MS if block_ms is None else block_ms
    response = await redis.xread({_key(session_id, "log"): f"{cursor}-0"}, count=batch, block=wait)
    if not response:
        return []
    _stream, entries = response[0]
    out: list[tuple[int, dict[str, Any]]] = []
    for entry_id, fields in entries:
        out.append((int(entry_id.split("-", 1)[0]), json.loads(fields["r"])))
    return out


# --- Presence and admission --------------------------------------------------------------------


async def _announce(redis: Redis, session_id: int, event_type: str, identity: dict[str, Any]) -> None:
    """Publish a presence transition as a table event. Best effort: presence never fails a request."""
    actor = ChannelActor(
        user_id=identity["user_id"] or None,
        display_name=identity.get("display_name"),
        character_id=identity.get("character_id"),
        character_name=identity.get("character_name"),
    )
    try:
        await publish(
            redis,
            session_id,
            kind=event_type,
            actor=actor,
            turn_id=None,
            data={"user_id": identity["user_id"], "state": event_type.split(".", 1)[1]},
        )
    except Exception:
        log_failure(
            "session_presence_publish_failed",
            classify_error("internal_fault"),
            level="warning",
            session_id=session_id,
        )


async def _live(redis: Redis, session_id: int, now: float) -> list[tuple[str, dict[str, Any]]]:
    """Live connections as (connection id, identity). Entries whose identity is missing are dropped."""
    # Alive means expiry strictly after now; sweep removes entries whose expiry is at or before now.
    conn_ids = await redis.zrangebyscore(_key(session_id, "live"), f"({now}", "+inf")
    if not conn_ids:
        return []
    metas = await redis.hmget(_key(session_id, "conns"), conn_ids)
    return [(conn, json.loads(meta)) for conn, meta in zip(conn_ids, metas, strict=True) if meta]


async def sweep(redis: Redis, session_id: int, *, now: float | None = None) -> list[int]:
    """Drop expired connections and announce members who have no live connection left.

    Returns the user ids that left through expiry. Presence events are published here.
    """
    t = _now(now)
    expired = await redis.zrangebyscore(_key(session_id, "live"), "-inf", t)
    if not expired:
        return []
    removed_users: set[int] = set()
    for conn_id in expired:
        if await redis.zrem(_key(session_id, "live"), conn_id):
            meta = await redis.hget(_key(session_id, "conns"), conn_id)
            await redis.hdel(_key(session_id, "conns"), conn_id)
            if meta:
                removed_users.add(int(json.loads(meta)["user_id"]))
    left: list[int] = []
    still_live = {int(meta["user_id"]) for _conn, meta in await _live(redis, session_id, t)}
    for user_id in sorted(removed_users - still_live):
        identity = {"user_id": user_id, "display_name": None}
        await redis.hdel(_key(session_id, "state"), str(user_id))
        await _announce(redis, session_id, "presence.left", identity)
        left.append(user_id)
    return left


async def admit(redis: Redis, session_id: int, user_id: int, *, now: float | None = None) -> None:
    """Refuse a new subscription when the session or this user is at its connection limit."""
    live = await _live(redis, session_id, _now(now))
    if len(live) >= MAX_SUBSCRIBERS_PER_SESSION:
        raise SubscriptionLimitError("session_subscribers_full")
    own = sum(1 for _conn, meta in live if int(meta["user_id"]) == user_id)
    if own >= MAX_CONNECTIONS_PER_USER_SESSION:
        raise SubscriptionLimitError("user_connections_full")


async def connect(
    redis: Redis,
    session_id: int,
    conn_id: str,
    identity: dict[str, Any],
    *,
    now: float | None = None,
) -> None:
    """Register a live connection. ``identity`` holds only user id, display name and seated PC."""
    t = _now(now)
    await sweep(redis, session_id, now=t)
    user_id = int(identity["user_id"])
    others = [meta for _conn, meta in await _live(redis, session_id, t) if int(meta["user_id"]) == user_id]
    await redis.zadd(_key(session_id, "live"), {conn_id: t + PRESENCE_TTL_SECONDS})
    await redis.hset(_key(session_id, "conns"), conn_id, json.dumps(identity, separators=(",", ":")))
    if not others:
        await _announce(redis, session_id, "presence.joined", identity)


async def heartbeat(
    redis: Redis,
    session_id: int,
    conn_id: str,
    identity: dict[str, Any],
    *,
    now: float | None = None,
) -> None:
    """Extend a live connection's expiry. A connection that already expired is registered again."""
    t = _now(now)
    score = await redis.zscore(_key(session_id, "live"), conn_id)
    if score is None or score <= t:
        # Already expired (or swept): announce any leave first, then register the connection again.
        await sweep(redis, session_id, now=t)
        await connect(redis, session_id, conn_id, identity, now=t)
    else:
        await redis.zadd(_key(session_id, "live"), {conn_id: t + PRESENCE_TTL_SECONDS}, xx=True)
    await sweep(redis, session_id, now=t)


async def disconnect(
    redis: Redis,
    session_id: int,
    conn_id: str,
    identity: dict[str, Any],
    *,
    now: float | None = None,
) -> None:
    """Remove a connection. The member is announced as left when this was their last connection."""
    t = _now(now)
    await redis.zrem(_key(session_id, "live"), conn_id)
    await redis.hdel(_key(session_id, "conns"), conn_id)
    user_id = int(identity["user_id"])
    if not any(int(meta["user_id"]) == user_id for _conn, meta in await _live(redis, session_id, t)):
        await redis.hdel(_key(session_id, "state"), str(user_id))
        await _announce(redis, session_id, "presence.left", identity)


async def set_state(
    redis: Redis,
    session_id: int,
    user_id: int,
    state: PresenceState,
    identity: dict[str, Any],
    *,
    now: float | None = None,
) -> bool:
    """Record a member's attention state. Announces the change only when it differs. Returns True if changed."""
    t = _now(now)
    previous = await redis.hget(_key(session_id, "state"), str(user_id))
    if (previous or "active") == state:
        return False
    await redis.hset(_key(session_id, "state"), str(user_id), state)
    if any(int(meta["user_id"]) == user_id for _conn, meta in await _live(redis, session_id, t)):
        await _announce(redis, session_id, f"presence.{state}", identity)
    return True


async def roster(redis: Redis, session_id: int, *, now: float | None = None) -> list[PresenceEntry]:
    """Members with at least one live connection, one entry each, in first-connected order."""
    t = _now(now)
    await sweep(redis, session_id, now=t)
    states = await redis.hgetall(_key(session_id, "state"))
    seen: dict[int, PresenceEntry] = {}
    for _conn, meta in await _live(redis, session_id, t):
        user_id = int(meta["user_id"])
        if user_id in seen:
            continue
        seen[user_id] = PresenceEntry(
            user_id=user_id,
            display_name=meta.get("display_name"),
            character_id=meta.get("character_id"),
            character_name=meta.get("character_name"),
            state=states.get(str(user_id), "active"),
        )
    return list(seen.values())
