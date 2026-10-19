"""Queued intended actions: submit, edit, cancel, read, announce (EDG-051).

Every write runs inside one transaction that locks the session row (``SELECT ... FOR UPDATE``),
the same lock the EDG-048 spotlight takes. Unlike a spotlight change it does NOT take the Redis turn
lock: waiting players queue precisely while another player's turn is running. A queued action never
touches ``spotlight_version`` or the holder, so queueing cannot reorder or duplicate a turn. It
becomes a turn only through ``spotlight.open_turn`` (``QueuedClaim``), which validates it again.

Writes are idempotent: a retried submit with the same ``request_id`` returns the same item, a repeated
edit with unchanged text is a no-op, and cancelling an item that is already gone succeeds.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

import structlog
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

import db.postgres.session as db_session_module

from ..schemas.channel import ChannelActor
from . import pacing, session_channel, spotlight
from .safe_errors import classify_error, log_failure
from .spotlight import SpotlightRefused

log = structlog.get_logger()
_PUBLISH_TIMEOUT_SECONDS = 2.0


def _character_of(table: spotlight.Table, user_id: int) -> int | None:
    """The hero this player plays at the table: their seat, else a hero the owner controls."""
    seat = table.seat_of(user_id)
    if seat is not None:
        return seat
    for character_id in table.rotation:
        if table.members[character_id].controller_id == user_id:
            return character_id
    return None


async def _lock_and_load(
    db: AsyncSession, redis: Redis, session_id: int, caller: int, now: datetime
):
    session = await spotlight._lock_session(db, session_id)
    table = await spotlight.load_table(db, redis, session, now=now, present_extra={caller})
    return session, table


async def submit(
    db: AsyncSession,
    redis: Redis,
    session_id: int,
    caller: int,
    *,
    text: str,
    request_id: str | None,
    spotlight_version: int | None,
    now: datetime | None = None,
) -> tuple[dict[str, Any], bool]:
    """Queue the caller's one intended action. Returns the stored item and whether anything changed."""
    t = now or spotlight._utc(None)
    session, table = await _lock_and_load(db, redis, session_id, caller, t)
    try:
        purged = pacing.purge_expired(session, t)
        existing = pacing.get_item(session, caller)
        if existing is not None:
            if request_id is not None and existing.get("request_id") == request_id:
                await db.commit()
                return existing, purged
            raise SpotlightRefused("queued_action_exists")
        character_id = _character_of(table, caller)
        if not table.engaged() or character_id is None or not table.members[character_id].eligible:
            raise SpotlightRefused("queue_unavailable")
        if spotlight_version is not None and spotlight_version != session.spotlight_version:
            raise SpotlightRefused("spotlight_stale")
        holder = table.holder
        if holder is None or table.members.get(holder) is None or table.members[holder].controller_id == caller:
            raise SpotlightRefused("queue_not_needed")
        item = pacing.new_item(
            session,
            user_id=caller,
            character_id=character_id,
            text=text,
            basis=await pacing.basis_of(db, session, character_id),
            request_id=request_id,
            now=t,
        )
        items = dict(pacing._store(session)["items"])
        items[str(caller)] = item
        pacing.save(session, items)
        await db.commit()
        return item, True
    except SpotlightRefused:
        await db.rollback()
        raise


async def edit(
    db: AsyncSession,
    redis: Redis,
    session_id: int,
    caller: int,
    action_id: str,
    *,
    text: str,
    expected_version: int,
    now: datetime | None = None,
) -> tuple[dict[str, Any], bool]:
    """Replace the text of the caller's own queued action, re-baselining it on the situation as it is now."""
    t = now or spotlight._utc(None)
    session, table = await _lock_and_load(db, redis, session_id, caller, t)
    try:
        pacing.purge_expired(session, t)
        item = pacing.get_item(session, caller)
        if item is None or item.get("id") != action_id:
            raise SpotlightRefused("queued_action_missing")
        if item["version"] == expected_version + 1 and item["text"] == text:
            await db.commit()  # the same edit, retried
            return item, False
        if item["version"] != expected_version:
            raise SpotlightRefused("queued_action_changed")
        character_id = _character_of(table, caller)
        if character_id is None or character_id != item["character_id"]:
            raise SpotlightRefused("queued_action_stale")
        updated = {
            **item,
            "text": text,
            "version": item["version"] + 1,
            "basis": await pacing.basis_of(db, session, character_id),
            "updated_at": t.isoformat(),
            "expires_at": (t + timedelta(seconds=pacing.policy_of(session).queue_ttl_seconds)).isoformat(),
        }
        items = dict(pacing._store(session)["items"])
        items[str(caller)] = updated
        pacing.save(session, items)
        await db.commit()
        return updated, True
    except SpotlightRefused:
        await db.rollback()
        raise


async def cancel(
    db: AsyncSession,
    redis: Redis,
    session_id: int,
    caller: int,
    action_id: str,
    *,
    owner_id: int,
    now: datetime | None = None,
) -> bool:
    """Remove a queued action: its author, or the campaign owner (to unblock the table). Idempotent."""
    t = now or spotlight._utc(None)
    session = await spotlight._lock_session(db, session_id)
    try:
        pacing.purge_expired(session, t)
        item = pacing.find_item(session, action_id)
        if item is None:
            await db.commit()
            return False
        if item["user_id"] != caller and caller != owner_id:
            raise SpotlightRefused("queued_action_missing")
        items = dict(pacing._store(session)["items"])
        items.pop(str(item["user_id"]))
        pacing.save(session, items)
        await db.commit()
        return True
    except SpotlightRefused:
        await db.rollback()
        raise


async def read(
    db: AsyncSession, redis: Redis, session_id: int, viewer_user_id: int | None, *, now: datetime | None = None
) -> dict[str, Any]:
    view = await spotlight.public_view(db, redis, session_id, now=now, viewer_user_id=viewer_user_id)
    return view["queue"]


async def announce(
    redis: Redis,
    session_id: int,
    reason: str,
    *,
    actor: ChannelActor | None = None,
) -> None:
    """Publish ``turn.queue_changed`` (redacted queue, no text) to the table. Best effort; never raises."""
    try:
        async with db_session_module.async_session_factory() as db:
            queue = await read(db, redis, session_id, None)
        await asyncio.wait_for(
            session_channel.publish(
                redis,
                session_id,
                kind=pacing.EVENT_TYPE,
                actor=actor,
                turn_id=None,
                data={"reason": reason, **queue},
            ),
            timeout=_PUBLISH_TIMEOUT_SECONDS,
        )
    except Exception:
        log_failure(
            "session_queue_publish_failed",
            classify_error("internal_fault"),
            level="warning",
            session_id=session_id,
        )
