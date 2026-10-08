"""Session router: CRUD plus the two turn endpoints (sync JSON and SSE stream).

Both turn endpoints take the per-session turn lock FIRST, then build the agent's initial state
with `_load_turn_initial_state` (so the state they act on cannot be stale), run the same agent
topology, and persist through `services.turn_runner.persist_turn` (one transaction per turn).
The rolling memory summary is updated as a background task after the response.
"""

from datetime import UTC, datetime
from typing import Any

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.background import BackgroundTask

from db.postgres.models import Campaign, EventLog, Session as SessionModel

from ..dependencies import acquire_turn_lock, get_redis, get_session
from ..schemas.session import ChatMessageRead, SessionCreate, SessionRead, SessionUpdate
from ..schemas.turn import TurnRequest, TurnResponse
from ..services.character_assignments import character_play_state, load_active_assignment
from ..services.combat import load_active_combat
from ..services.npcs import load_npcs
from ..services.session_intro import generate_session_intro
from ..services.session_messages import (
    load_memory_summary,
    load_session_messages,
    maybe_update_memory_summary,
)
from ..services.turn_runner import (
    TurnError,
    combat_state_public,
    error_body,
    persist_turn,
    run_turn,
    stream_session_turn,
)
from ..services.world_flags import load_world_flags

log = structlog.get_logger()

_BUSY_DETAIL = "A turn is already in progress for this session. Please retry."

router = APIRouter(tags=["sessions"])


async def _load_turn_initial_state(
    session_id: int,
    message: str,
    db: AsyncSession,
) -> dict[str, Any]:
    """Build the agent's initial state from DB rows. Raises 404 if the session is gone.

    Call only while holding the turn lock. Ends the read-only transaction before returning so
    the request's pooled connection is not held across the turn's LLM calls (the turn's writes
    use their own short-lived session in `persist_turn`).
    """
    result = await db.execute(select(SessionModel).where(SessionModel.id == session_id))
    session = result.scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    camp_result = await db.execute(select(Campaign).where(Campaign.id == session.campaign_id))
    campaign = camp_result.scalar_one_or_none()
    if campaign is None:
        raise HTTPException(status_code=404, detail="Campaign not found for session")
    if campaign.status == "ended":
        raise HTTPException(
            status_code=409,
            detail="Campaign has ended; reopen it to continue.",
        )
    world_flags = await load_world_flags(db, session.campaign_id)

    prior_messages = await load_session_messages(db, session_id)
    character = None
    if session.active_character_id:
        character = await character_play_state(
            db, session.active_character_id, session.campaign_id
        )
    npcs = await load_npcs(db, session.campaign_id)
    npc_summaries = [{"name": n.name, "disposition": n.disposition} for n in npcs]

    initial_state: dict[str, Any] = {
        "player_input": message,
        "session_id": session_id,
        "campaign_id": session.campaign_id,
        "messages": prior_messages,
        "adventure_collections": list(campaign.adventure_collections or []),
        "world_flags": world_flags,
        "current_scene_id": session.current_scene_id,
        "character": character,
        "npcs": npc_summaries,
        "memory_summary": await load_memory_summary(db, session_id),
    }
    combat = await load_active_combat(db, session_id)
    if combat:
        initial_state["combat_state"] = combat
    await db.commit()
    return initial_state


@router.get("/sessions", response_model=list[SessionRead])
async def list_sessions(
    campaign_id: int | None = None,
    db: AsyncSession = Depends(get_session),
):
    """List sessions. Optional ?campaign_id= filter."""
    q = select(SessionModel)
    if campaign_id is not None:
        q = q.where(SessionModel.campaign_id == campaign_id)
    result = await db.execute(q)
    sessions = result.scalars().all()
    return [SessionRead.model_validate(s) for s in sessions]


@router.get("/sessions/{session_id}", response_model=SessionRead)
async def get_session_by_id(session_id: int, db: AsyncSession = Depends(get_session)):
    result = await db.execute(select(SessionModel).where(SessionModel.id == session_id))
    session = result.scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return SessionRead.model_validate(session)


@router.get("/sessions/{session_id}/combat")
async def get_session_combat(session_id: int, db: AsyncSession = Depends(get_session)):
    """Active encounter in the same public shape as the stream's `done.combat_state`, or null.

    Lets the client restore the combat HUD after a reload.
    """
    exists = await db.execute(select(SessionModel.id).where(SessionModel.id == session_id))
    if exists.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return combat_state_public(await load_active_combat(db, session_id))


@router.get("/sessions/{session_id}/messages", response_model=list[ChatMessageRead])
async def list_session_messages(session_id: int, db: AsyncSession = Depends(get_session)):
    """Hydrate chat UI from narration rows in event_log (oldest first)."""
    result = await db.execute(select(SessionModel).where(SessionModel.id == session_id))
    if result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Session not found")

    ev_result = await db.execute(
        select(EventLog)
        .where(EventLog.session_id == session_id, EventLog.event_type == "narration")
        .order_by(EventLog.created_at.asc())
    )
    events = ev_result.scalars().all()
    out: list[ChatMessageRead] = []
    for e in events:
        p = e.payload or {}
        if p.get("player_input"):
            out.append(
                ChatMessageRead(
                    role="user",
                    content=str(p["player_input"]),
                    created_at=e.created_at,
                )
            )
        if p.get("narration"):
            out.append(
                ChatMessageRead(
                    role="dm",
                    content=str(p["narration"]),
                    created_at=e.created_at,
                )
            )
    return out


@router.post("/sessions/{session_id}/intro")
async def session_intro(
    session_id: int,
    db: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
):
    """Generate the opening scene for a fresh session (idempotent).

    The UI calls this when a session has no history yet, so the player lands on a DM-set scene
    instead of a blank chat. Returns `{started: true, narration, character, current_scene_id}`
    when it created the opening, or `{started: false}` if the session already has narration.
    """
    # Reuse the per-session turn lock so an intro can't race a first turn (or another intro).
    # Taken before reading the session so the intro acts on current state.
    lock = await acquire_turn_lock(session_id, redis)
    if lock is None:
        raise HTTPException(status_code=409, detail="Session is busy; please retry.")
    try:
        result = await db.execute(select(SessionModel).where(SessionModel.id == session_id))
        session = result.scalar_one_or_none()
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")

        camp_res = await db.execute(select(Campaign).where(Campaign.id == session.campaign_id))
        campaign = camp_res.scalar_one_or_none()
        if campaign is not None and campaign.status == "ended":
            raise HTTPException(
                status_code=409, detail="Campaign has ended; reopen it to continue."
            )

        payload = await generate_session_intro(db, session)
    finally:
        await lock.release()

    if payload is None:
        return {"started": False}
    return {"started": True, **payload}


@router.post("/sessions", response_model=SessionRead)
async def create_session(body: SessionCreate, db: AsyncSession = Depends(get_session)):
    campaign = await db.get(Campaign, body.campaign_id)
    if campaign is None:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if body.active_character_id is not None:
        ass = await load_active_assignment(db, body.active_character_id, body.campaign_id)
        if ass is None:
            raise HTTPException(
                status_code=422,
                detail="Character has no active assignment in this campaign",
            )
    started_at = body.started_at if body.started_at is not None else datetime.now(UTC)
    session = SessionModel(
        campaign_id=body.campaign_id,
        started_at=started_at,
        ended_at=body.ended_at,
        active_character_id=body.active_character_id,
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return SessionRead.model_validate(session)


@router.patch("/sessions/{session_id}", response_model=SessionRead)
async def update_session(
    session_id: int, body: SessionUpdate, db: AsyncSession = Depends(get_session)
):
    result = await db.execute(select(SessionModel).where(SessionModel.id == session_id))
    session = result.scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    update_data = body.model_dump(exclude_unset=True)
    # Same invariant create enforces: the active character must be playing in this campaign.
    new_character_id = update_data.get("active_character_id")
    if new_character_id is not None:
        ass = await load_active_assignment(db, new_character_id, session.campaign_id)
        if ass is None:
            raise HTTPException(
                status_code=422,
                detail="Character has no active assignment in this campaign",
            )
    for key, value in update_data.items():
        setattr(session, key, value)
    await db.commit()
    await db.refresh(session)
    return SessionRead.model_validate(session)


@router.delete("/sessions/{session_id}", status_code=204)
async def delete_session(session_id: int, db: AsyncSession = Depends(get_session)):
    result = await db.execute(select(SessionModel).where(SessionModel.id == session_id))
    session = result.scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    await db.delete(session)
    await db.commit()


def _turn_error_response(err: TurnError) -> JSONResponse:
    return JSONResponse(status_code=err.status_code, content=error_body(err.code))


@router.post("/sessions/{session_id}/turn", response_model=TurnResponse)
async def session_turn(
    session_id: int,
    body: TurnRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
):
    """Run one player turn synchronously and return the full result.

    Lifecycle: acquire Redis lock (409 on contention) -> load state -> run agent -> persist
    the whole turn in one transaction -> release lock in `finally` -> update the rolling memory
    summary in the background. Failures return `{"code", "message", "detail"}` with a generic
    message (409 missing assignment, 503 agent/LLM failure, 500 otherwise); details are logged.
    """
    lock = await acquire_turn_lock(session_id, redis)
    if lock is None:
        raise HTTPException(status_code=409, detail=_BUSY_DETAIL)

    structlog.contextvars.bind_contextvars(session_id=session_id)
    try:
        initial_state = await _load_turn_initial_state(session_id, body.message, db)
        structlog.contextvars.bind_contextvars(campaign_id=initial_state["campaign_id"])

        result = await run_turn(initial_state)
        done = await persist_turn(session_id, body.message, result)
        background_tasks.add_task(maybe_update_memory_summary, session_id)

        adjudication = result.get("adjudication_result")
        return TurnResponse(
            narration=done["narration"],
            adjudication=adjudication.model_dump() if adjudication else None,
            combat_state=result.get("combat_state"),
            current_scene_id=done["current_scene_id"],
            character=done["character"],
            campaign_complete=done["campaign_complete"],
        )
    except HTTPException:
        raise
    except TurnError as e:
        return _turn_error_response(e)
    except Exception:
        log.exception("turn_failed", session_id=session_id)
        return _turn_error_response(TurnError("turn_failed"))
    finally:
        structlog.contextvars.unbind_contextvars("session_id", "campaign_id")
        await lock.release()


@router.post("/sessions/{session_id}/turn/stream")
async def session_turn_stream(
    session_id: int,
    body: TurnRequest,
    db: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
):
    """Stream the turn as Server-Sent Events; the lock is held for the lifetime of the stream.

    A 409 (lock contention) or 404 is returned as a regular JSON error before the stream opens.
    Errors after the stream begins are emitted as `error` SSE frames; the connection then
    closes and the lock is released in the generator's `finally`.
    """
    lock = await acquire_turn_lock(session_id, redis)
    if lock is None:
        raise HTTPException(status_code=409, detail=_BUSY_DETAIL)
    try:
        initial_state = await _load_turn_initial_state(session_id, body.message, db)
    except BaseException:
        await lock.release()
        raise

    async def event_gen():
        structlog.contextvars.bind_contextvars(
            session_id=session_id,
            campaign_id=initial_state["campaign_id"],
        )
        try:
            async for chunk in stream_session_turn(
                session_id=session_id,
                body_message=body.message,
                initial_state=initial_state,
                debug=body.debug,
            ):
                yield chunk
        finally:
            structlog.contextvars.unbind_contextvars("session_id", "campaign_id")
            await lock.release()

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
        # Runs after the stream closes (and the lock is released); never fails the turn.
        background=BackgroundTask(maybe_update_memory_summary, session_id),
    )
