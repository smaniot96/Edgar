"""The live session channel over HTTP (EDG-047): an SSE subscription and presence.

``GET /api/sessions/{id}/events`` is a Server-Sent Events stream of the session's ordered channel.
The client sends its bearer token in the ``Authorization`` header (fetch streaming, never a URL),
and resumes with ``Last-Event-ID``. Input stays on the existing POST turn routes; this router only
carries server-to-client fan-out and presence. See docs/session-broadcast.md for the protocol.

Authorization runs through the EDG-046 helpers on every resolution: a non-member gets the same 404
as a missing session, a spectator gets the table channels only, and a member removed mid-stream is
cut off as soon as the membership change is announced (and within ``RECHECK_SECONDS`` regardless).
No database connection is held for the life of a stream: each check opens and closes a short
session. Only the subscriber's own presence identity (user id, display name, seated character)
is ever written to Redis.
"""

from __future__ import annotations

import uuid
from contextlib import aclosing
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.background import BackgroundTask

import db.postgres.session as db_session_module
from db.postgres.models import (
    Campaign,
    Character,
    Session as SessionModel,
    SessionParticipant,
    User,
)

from ..auth import Auth, campaign_role, member_session
from ..dependencies import get_redis, get_session
from ..schemas.channel import PresenceEntry, PresenceUpdate
from ..services import session_channel, spotlight
from ..services.character_assignments import character_play_state
from ..services.combat import load_active_combat
from ..services.session_channel import (
    OWNER_ROLE,
    PLAYER_ROLE,
    SubscriptionLimitError,
    Viewer,
)
from ..services.session_stream import acquire_slot, stream_frames
from ..services.turn_runner import combat_state_public

router = APIRouter(tags=["sessions"])

_LIMIT_MESSAGE = "Too many live connections for this session. Close another tab and retry."


def _last_event_id(raw: str | None) -> int | None:
    """The client's last-seen sequence number, or None for a new subscriber."""
    if raw is None or raw == "":
        return None
    if not raw.isdigit() or len(raw) > 18:
        raise HTTPException(status_code=400, detail="Invalid Last-Event-ID.")
    return int(raw)


def _user_id(auth: Auth) -> int:
    """Presence keys need an integer. Single-user local mode has no account, so it uses 0."""
    return 0 if auth is None else auth.user_id


async def _identity(db: AsyncSession, session_id: int, auth: Auth) -> dict[str, Any]:
    """Presence identity: user id, display name and the PC seated here. Nothing else."""
    user_id = _user_id(auth)
    display_name = None
    if auth is not None:
        user = await db.get(User, user_id)
        display_name = user.display_name if user is not None else None
    seat = (
        await db.execute(
            select(SessionParticipant.character_id, Character.name)
            .join(Character, Character.id == SessionParticipant.character_id)
            .where(
                SessionParticipant.session_id == session_id,
                SessionParticipant.user_id == user_id,
            )
        )
    ).first()
    return {
        "user_id": user_id,
        "display_name": display_name,
        "character_id": seat[0] if seat is not None else None,
        "character_name": seat[1] if seat is not None else None,
    }


async def _resolve_viewer(session_id: int, auth: Auth, *, strict: bool) -> Viewer | None:
    """The caller's role for this session, from a short-lived database session.

    ``strict`` raises the membership 404 (subscription start). Otherwise a lost membership returns
    None (periodic re-check), which ends the stream.
    """
    async with db_session_module.async_session_factory() as db:
        try:
            session = await member_session(db, session_id, auth)
        except HTTPException:
            if strict:
                raise
            return None
        if auth is None:
            # Single-user local mode: the one local user is the owner of everything.
            return Viewer(user_id=None, role=OWNER_ROLE)
        campaign = await db.get(Campaign, session.campaign_id)
        role = await campaign_role(db, campaign, auth.user_id) if campaign is not None else None
        if role is None:
            if strict:
                raise HTTPException(status_code=404, detail="Session not found")
            return None
        return Viewer(user_id=auth.user_id, role=role)


class DbSubscriber:
    """The request's view of the channel, re-resolved from the database on every check."""

    def __init__(self, *, redis: Redis, session_id: int, auth: Auth) -> None:
        self._redis = redis
        self._session_id = session_id
        self._auth = auth

    async def viewer(self) -> Viewer | None:
        return await _resolve_viewer(self._session_id, self._auth, strict=False)

    async def identity(self) -> dict[str, Any]:
        async with db_session_module.async_session_factory() as db:
            return await _identity(db, self._session_id, self._auth)

    async def snapshot(self, head: int, viewer: Viewer) -> dict[str, Any]:
        """Shared scene and encounter state. A seated viewer also gets their own character."""
        async with db_session_module.async_session_factory() as db:
            session = await db.get(SessionModel, self._session_id)
            if session is None:
                return {"head_seq": head, "session_id": self._session_id}
            holder_state = None
            if session.active_character_id is not None:
                holder_state = await character_play_state(
                    db, session.active_character_id, session.campaign_id
                )
            own_character = None
            if viewer.user_id is not None and viewer.role in (OWNER_ROLE, PLAYER_ROLE):
                own = (
                    await db.execute(
                        select(SessionParticipant.character_id).where(
                            SessionParticipant.session_id == session.id,
                            SessionParticipant.user_id == viewer.user_id,
                        )
                    )
                ).scalar_one_or_none()
                if own is not None:
                    own_character = await character_play_state(db, own, session.campaign_id)
            combat = combat_state_public(await load_active_combat(db, session.id))
            # EDG-048: the table's spotlight, so a reconnecting member sees the same next-up order.
            spotlight_state = await spotlight.public_view(
                db, self._redis, session.id, viewer_user_id=viewer.user_id
            )
        roster = await session_channel.roster(self._redis, self._session_id)
        return {
            "head_seq": head,
            "session_id": session.id,
            "current_scene_id": session.current_scene_id,
            "spotlight": (
                {"character_id": holder_state["id"], "character_name": holder_state["name"]}
                if holder_state is not None
                else None
            ),
            "combat_state": combat,
            "spotlight_state": spotlight_state,
            "presence": [entry.model_dump(mode="json") for entry in roster],
            "character": own_character,
        }


@router.get("/sessions/{session_id}/events")
async def session_events(
    session_id: int,
    request: Request,
    auth: Auth,
    redis: Redis = Depends(get_redis),
):
    """Subscribe to the session channel as Server-Sent Events, resuming after ``Last-Event-ID``."""
    last_event_id = _last_event_id(request.headers.get("last-event-id"))
    viewer = await _resolve_viewer(session_id, auth, strict=True)
    slot = acquire_slot()
    if slot is None:
        raise HTTPException(
            status_code=429,
            detail={"code": "subscription_limit", "message": _LIMIT_MESSAGE, "reason": "process_full"},
        )
    try:
        await session_channel.admit(redis, session_id, _user_id(auth))
    except SubscriptionLimitError as exc:
        slot.release()
        raise HTTPException(
            status_code=429,
            detail={"code": "subscription_limit", "message": _LIMIT_MESSAGE, "reason": exc.code},
        ) from exc
    subscriber = DbSubscriber(redis=redis, session_id=session_id, auth=auth)
    conn_id = uuid.uuid4().hex

    async def body():
        try:
            async with aclosing(
                stream_frames(
                    redis=redis,
                    session_id=session_id,
                    subscriber=subscriber,
                    viewer=viewer,
                    last_event_id=last_event_id,
                    conn_id=conn_id,
                )
            ) as frames:
                async for chunk in frames:
                    yield chunk
        finally:
            slot.release()

    return StreamingResponse(
        body(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        # A client that disconnects before the body starts never runs the generator's finally.
        background=BackgroundTask(slot.release),
    )


@router.get("/sessions/{session_id}/presence", response_model=list[PresenceEntry])
async def list_presence(
    session_id: int,
    auth: Auth,
    db: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
):
    """Who is at the table right now: display name, seated character and attention state."""
    await member_session(db, session_id, auth)
    return await session_channel.roster(redis, session_id)


@router.post("/sessions/{session_id}/presence", status_code=204)
async def set_presence(
    session_id: int,
    body: PresenceUpdate,
    auth: Auth,
    db: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
):
    """Report this member's attention (``active`` or ``idle``). Any member may report for themselves."""
    await member_session(db, session_id, auth)
    identity = await _identity(db, session_id, auth)
    await session_channel.set_state(
        redis, session_id, _user_id(auth), body.state, identity
    )
