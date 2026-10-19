"""The subscriber loop for the session channel: ordered delivery, replay, re-checks and bounds (EDG-047).

One loop serves both live delivery and reconnect replay. It reads the session log after a cursor
(the client's last-seen sequence number, or the head for a new subscriber), applies the delivery
rule to every record, and yields SSE frames. Access is re-resolved through the ``Subscriber``
before the first frame and again every ``RECHECK_SECONDS`` or as soon as an ``access.changed``
record is read, so a removed member is cut off at once and a spectator never receives a player
record. Nothing here touches HTTP or SQL: the route supplies a ``Subscriber`` backed by the EDG-046
membership helpers, and the tests supply a fake one.

Frames (SSE, UTF-8):

* ``id: <seq>`` + ``event: <type>`` + ``data: <envelope JSON>`` for every delivered record.
* ``event: snapshot`` (``id`` = head) when a new subscriber asks for no history, or when the client
  is behind the retained window or ahead of the log. The client then re-reads its history.
* ``event: closed`` (no ``id``) with a reason, then the stream ends. ``access_revoked`` must not be
  retried; the others may reconnect with the last ``id`` they saw.
* ``: ping`` comments as heartbeats, and one ``retry:`` hint at the start.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any, Protocol

import structlog
from redis.asyncio import Redis

from apps.api.schemas.channel import AUDIENCE_CONTROL, ChannelEnvelope

from . import session_channel
from .session_channel import (
    HEARTBEAT_SECONDS,
    MAX_STREAM_SECONDS,
    MAX_STREAMS_PER_PROCESS,
    RECHECK_SECONDS,
    SLOW_CONSUMER_SECONDS,
    Viewer,
    audience_visible,
)

log = structlog.get_logger()

RETRY_MILLISECONDS = 3000

_draining = False
_open_streams = 0


class Subscriber(Protocol):
    """What the loop needs from the request: current access, presence identity and a snapshot."""

    async def viewer(self) -> Viewer | None:
        """The caller's current role, or None once they are no longer a member."""

    async def identity(self) -> dict[str, Any]:
        """Presence identity: user id, display name and seated character. Nothing else."""

    async def snapshot(self, head: int, viewer: Viewer) -> dict[str, Any]:
        """Shared scene and encounter state, plus the viewer's own character when seated."""


def begin_drain() -> None:
    """Ask every open stream to end with ``server_shutdown`` (called from the app lifespan)."""
    global _draining
    _draining = True


def reset_drain() -> None:
    global _draining
    _draining = False


def is_draining() -> bool:
    return _draining


class StreamSlot:
    """One process-wide stream slot. ``release`` is idempotent, so both the generator and a background task may call it."""

    def __init__(self) -> None:
        self._released = False

    def release(self) -> None:
        global _open_streams
        if not self._released:
            self._released = True
            _open_streams = max(0, _open_streams - 1)


def acquire_slot() -> StreamSlot | None:
    """A slot for one more open stream, or None when this process is at its limit."""
    global _open_streams
    if _open_streams >= MAX_STREAMS_PER_PROCESS:
        return None
    _open_streams += 1
    return StreamSlot()


def open_stream_count() -> int:
    return _open_streams


def sse_frame(event: str, data: dict[str, Any], *, frame_id: int | None = None) -> str:
    lines = []
    if frame_id is not None:
        lines.append(f"id: {frame_id}")
    lines.append(f"event: {event}")
    lines.append("data: " + json.dumps(data, separators=(",", ":")))
    return "\n".join(lines) + "\n\n"


def _closed(reason: str) -> str:
    return sse_frame("closed", {"reason": reason})


async def _behind(redis: Redis, session_id: int, cursor: int, head: int) -> bool:
    """True when records after ``cursor`` were trimmed away, so replay cannot be complete."""
    oldest = await session_channel.oldest_seq(redis, session_id)
    if oldest is None:
        return head > cursor
    return oldest > cursor + 1


async def _snapshot_frame(subscriber: Subscriber, head: int, viewer: Viewer) -> str:
    data = await subscriber.snapshot(head, viewer)
    envelope = ChannelEnvelope(
        seq=head,
        type="snapshot",
        channel="system",
        at=datetime.now(UTC),
        data=data,
    )
    return sse_frame("snapshot", envelope.model_dump(mode="json"), frame_id=head)


async def stream_frames(
    *,
    redis: Redis,
    session_id: int,
    subscriber: Subscriber,
    viewer: Viewer,
    last_event_id: int | None,
    conn_id: str,
    clock: Callable[[], float] = time.monotonic,
) -> AsyncIterator[str]:
    """Yield the frames for one subscription until it ends. The caller owns admission and the slot."""
    identity = await subscriber.identity()
    await session_channel.connect(redis, session_id, conn_id, identity)
    try:
        yield f"retry: {RETRY_MILLISECONDS}\n\n"
        head = await session_channel.head_seq(redis, session_id)
        cursor = last_event_id
        if cursor is None or cursor > head or await _behind(redis, session_id, cursor, head):
            yield await _snapshot_frame(subscriber, head, viewer)
            cursor = head
        started = last_beat = last_check = clock()
        control_seen = False
        while True:
            if is_draining():
                yield _closed("server_shutdown")
                return
            now = clock()
            if now - started >= MAX_STREAM_SECONDS:
                yield _closed("lifetime")
                return
            if control_seen or now - last_check >= RECHECK_SECONDS:
                control_seen = False
                last_check = now
                current = await subscriber.viewer()
                if current is None:
                    log.info("session_stream_access_revoked", session_id=session_id)
                    yield _closed("access_revoked")
                    return
                viewer = current
            if now - last_beat >= HEARTBEAT_SECONDS:
                last_beat = now
                await session_channel.heartbeat(redis, session_id, conn_id, identity)
                yield ": ping\n\n"
            head = await session_channel.head_seq(redis, session_id)
            if cursor < head and await _behind(redis, session_id, cursor, head):
                log.info("session_stream_resync", session_id=session_id)
                yield await _snapshot_frame(subscriber, head, viewer)
                cursor = head
                continue
            for seq, record in await session_channel.read_after(redis, session_id, cursor):
                cursor = seq
                if record["audience"] == AUDIENCE_CONTROL:
                    # Stop delivering this batch: membership may have changed. The rest of the batch
                    # is read again after the re-check, from the cursor set above.
                    control_seen = True
                    break
                if not audience_visible(record["audience"], viewer):
                    continue
                envelope = record["envelope"]
                sent_at = clock()
                yield sse_frame(envelope["type"], envelope, frame_id=seq)
                # The generator resumes only after the transport accepted the frame, so this
                # measures how long the client took to drain it.
                if clock() - sent_at > SLOW_CONSUMER_SECONDS:
                    log.info("session_stream_slow_consumer", session_id=session_id)
                    yield _closed("slow_consumer")
                    return
    finally:
        with suppress(Exception):
            await session_channel.disconnect(redis, session_id, conn_id, identity)
