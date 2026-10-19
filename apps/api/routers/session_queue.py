"""Queued intended actions for a shared table (EDG-051).

A seated player whose turn is not up may queue ONE action. It is shown to them (text) and to the
table (that it is queued), can be edited or cancelled, expires, and is validated again when the
player's turn comes (see ``POST /sessions/{id}/turn`` with ``queued_action_id``). Nothing here
moves the spotlight. Single-user local mode has no table to wait at: every route answers 409
``queue_unavailable`` there, and reads return an empty queue.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from db.postgres.models import Campaign, User

from ..auth import Auth, member_session, turn_session
from ..dependencies import get_redis, get_session
from ..schemas.channel import ChannelActor
from ..schemas.pacing import QueuedActionCreate, QueuedActionEdit, QueuedActionRead, QueueRead
from ..services import queued_actions
from ..services.spotlight import SpotlightRefused
from ..services.turn_runner import TurnError

router = APIRouter(tags=["sessions"])


def _refused(exc: SpotlightRefused) -> JSONResponse:
    err = TurnError(exc.code, outcome="not_committed")
    return JSONResponse(status_code=err.status_code, content=err.body())


async def _actor(db: AsyncSession, auth: Auth) -> ChannelActor:
    user = await db.get(User, auth.user_id) if auth is not None else None
    return ChannelActor(
        user_id=None if auth is None else auth.user_id,
        display_name=user.display_name if user is not None else None,
    )


async def _item_view(
    db: AsyncSession, redis: Redis, session_id: int, auth: Auth, action_id: str
) -> QueuedActionRead | JSONResponse:
    queue = await queued_actions.read(db, redis, session_id, None if auth is None else auth.user_id)
    found = next((i for i in queue["items"] if i["id"] == action_id), None)
    if found is None:  # played or cancelled between the write and this read
        return _refused(SpotlightRefused("queued_action_missing"))
    return QueuedActionRead.model_validate(found)


@router.get("/sessions/{session_id}/queue", response_model=QueueRead)
async def get_queue(
    session_id: int,
    auth: Auth,
    db: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
):
    """The table's queued actions. Text is included only for the caller's own."""
    await member_session(db, session_id, auth)
    return await queued_actions.read(db, redis, session_id, None if auth is None else auth.user_id)


@router.post("/sessions/{session_id}/queue", response_model=QueuedActionRead, status_code=201)
async def queue_action(
    session_id: int,
    body: QueuedActionCreate,
    auth: Auth,
    response: Response,
    db: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
):
    """Queue the caller's one intended action while another player holds the spotlight."""
    await turn_session(db, session_id, auth)
    if auth is None:
        return _refused(SpotlightRefused("queue_unavailable"))
    try:
        item, changed = await queued_actions.submit(
            db,
            redis,
            session_id,
            auth.user_id,
            text=body.text,
            request_id=body.request_id,
            spotlight_version=body.spotlight_version,
        )
    except SpotlightRefused as exc:
        return _refused(exc)
    if changed:
        await queued_actions.announce(redis, session_id, "queued", actor=await _actor(db, auth))
    else:
        response.status_code = 200  # a retry of the same submission
    return await _item_view(db, redis, session_id, auth, item["id"])


@router.patch("/sessions/{session_id}/queue/{action_id}", response_model=QueuedActionRead)
async def edit_queued_action(
    session_id: int,
    action_id: str,
    body: QueuedActionEdit,
    auth: Auth,
    db: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
):
    """Edit the caller's own queued action. ``expected_version`` guards against a lost update."""
    await turn_session(db, session_id, auth)
    if auth is None:
        return _refused(SpotlightRefused("queue_unavailable"))
    try:
        item, changed = await queued_actions.edit(
            db,
            redis,
            session_id,
            auth.user_id,
            action_id,
            text=body.text,
            expected_version=body.expected_version,
        )
    except SpotlightRefused as exc:
        return _refused(exc)
    if changed:
        await queued_actions.announce(redis, session_id, "edited", actor=await _actor(db, auth))
    return await _item_view(db, redis, session_id, auth, item["id"])


@router.delete("/sessions/{session_id}/queue/{action_id}", status_code=204)
async def cancel_queued_action(
    session_id: int,
    action_id: str,
    auth: Auth,
    db: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
):
    """Cancel a queued action: its author, or the campaign owner. Cancelling a gone action succeeds."""
    session = await turn_session(db, session_id, auth)
    if auth is None:
        return _refused(SpotlightRefused("queue_unavailable"))
    campaign = await db.get(Campaign, session.campaign_id)
    try:
        removed = await queued_actions.cancel(
            db,
            redis,
            session_id,
            auth.user_id,
            action_id,
            owner_id=campaign.created_by if campaign is not None else 0,
        )
    except SpotlightRefused as exc:
        return _refused(exc)
    if removed:
        await queued_actions.announce(redis, session_id, "cancelled", actor=await _actor(db, auth))
    return Response(status_code=204)
