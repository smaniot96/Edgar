import structlog
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.postgres.models import Campaign, Character, EventLog, Session as SessionModel
from ..dependencies import acquire_turn_lock, get_redis, get_session, release_turn_lock
from ..schemas.session import ChatMessageRead, SessionCreate, SessionRead, SessionUpdate
from ..schemas.turn import TurnRequest, TurnResponse
from ..services.combat import load_active_combat, persist_combat
from ..services.session_messages import load_session_messages
from ..services.turn_runner import stream_session_turn
from ..services.world_flags import load_world_flags
from ..services.world_writes import apply_adjudication

router = APIRouter(tags=["sessions"])


async def _load_turn_initial_state(
    session_id: int,
    message: str,
    db: AsyncSession,
) -> tuple[SessionModel, dict[str, Any]]:
    """Load session, campaign, and agent initial state for a turn (shared by sync and SSE endpoints)."""
    result = await db.execute(select(SessionModel).where(SessionModel.id == session_id))
    session = result.scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    camp_result = await db.execute(select(Campaign).where(Campaign.id == session.campaign_id))
    campaign = camp_result.scalar_one_or_none()
    if campaign is None:
        raise HTTPException(status_code=404, detail="Campaign not found for session")
    world_flags = await load_world_flags(db, session.campaign_id)

    prior_messages = await load_session_messages(db, session_id)
    character = None
    if session.active_character_id:
        cresult = await db.execute(
            select(Character).where(Character.id == session.active_character_id)
        )
        char = cresult.scalar_one_or_none()
        if char is not None:
            character = {
                "id": char.id,
                "name": char.name,
                "class": char.character_class,
                "level": char.level,
                "hp_current": char.hp_current,
                "hp_max": char.hp_max,
                "stats": char.stats,
                "inventory": char.inventory,
            }
    initial_state: dict[str, Any] = {
        "player_input": message,
        "session_id": session_id,
        "campaign_id": session.campaign_id,
        "messages": prior_messages,
        "adventure_collections": list(campaign.adventure_collections or []),
        "world_flags": world_flags,
        "current_scene_id": session.current_scene_id,
        "character": character,
    }
    combat = await load_active_combat(db, session_id)
    if combat:
        initial_state["combat_state"] = combat
    return session, initial_state


@router.get("/sessions", response_model=list[SessionRead])
async def list_sessions(db: AsyncSession = Depends(get_session)):
    result = await db.execute(select(SessionModel))
    sessions = result.scalars().all()
    return [SessionRead.model_validate(s) for s in sessions]


@router.get("/sessions/{session_id}", response_model=SessionRead)
async def get_session_by_id(session_id: int, db: AsyncSession = Depends(get_session)):
    result = await db.execute(select(SessionModel).where(SessionModel.id == session_id))
    session = result.scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return SessionRead.model_validate(session)


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


@router.post("/sessions", response_model=SessionRead)
async def create_session(body: SessionCreate, db: AsyncSession = Depends(get_session)):
    session = SessionModel(campaign_id=body.campaign_id, ended_at=body.ended_at)
    if body.started_at is not None:
        session.started_at = body.started_at
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


@router.post("/sessions/{session_id}/turn", response_model=TurnResponse)
async def session_turn(
    session_id: int,
    body: TurnRequest,
    db: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
):
    """Process a player turn: acquire lock, invoke agent, return narration."""
    session, initial_state = await _load_turn_initial_state(session_id, body.message, db)
    structlog.contextvars.bind_contextvars(
        session_id=session_id,
        campaign_id=session.campaign_id,
    )

    acquired = await acquire_turn_lock(session_id, redis)
    if not acquired:
        structlog.contextvars.unbind_contextvars("session_id", "campaign_id")
        raise HTTPException(
            status_code=409,
            detail="A turn is already in progress for this session. Please retry.",
        )

    try:
        from agent.graph import app

        result = await app.ainvoke(initial_state)

        if result.get("error"):
            raise HTTPException(status_code=503, detail=result["error"])

        new_combat = result.get("combat_state")
        if new_combat:
            await persist_combat(db, session_id, new_combat)

        adjudication = result.get("adjudication_result")
        if adjudication:
            await apply_adjudication(db, session, adjudication)

        db.add(
            EventLog(
                session_id=session_id,
                event_type="narration",
                payload={
                    "player_input": body.message,
                    "narration": result.get("narration", ""),
                },
            )
        )
        await db.commit()

        await db.refresh(session)
        character_out = None
        if session.active_character_id:
            crefresh = await db.execute(
                select(Character).where(Character.id == session.active_character_id)
            )
            char_row = crefresh.scalar_one_or_none()
            if char_row is not None:
                character_out = {
                    "id": char_row.id,
                    "name": char_row.name,
                    "class": char_row.character_class,
                    "level": char_row.level,
                    "hp_current": char_row.hp_current,
                    "hp_max": char_row.hp_max,
                    "stats": char_row.stats,
                    "inventory": char_row.inventory,
                }

        return TurnResponse(
            narration=result.get("narration", ""),
            adjudication=adjudication.model_dump() if adjudication else None,
            combat_state=result.get("combat_state"),
            current_scene_id=session.current_scene_id,
            character=character_out,
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        structlog.contextvars.unbind_contextvars("session_id", "campaign_id")
        await release_turn_lock(session_id, redis)


@router.post("/sessions/{session_id}/turn/stream")
async def session_turn_stream(
    session_id: int,
    body: TurnRequest,
    db: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
):
    """Process a turn with Server-Sent Events (status, token stream, adjudication, done)."""
    session, initial_state = await _load_turn_initial_state(session_id, body.message, db)
    structlog.contextvars.bind_contextvars(
        session_id=session_id,
        campaign_id=session.campaign_id,
    )

    acquired = await acquire_turn_lock(session_id, redis)
    if not acquired:
        structlog.contextvars.unbind_contextvars("session_id", "campaign_id")
        raise HTTPException(
            status_code=409,
            detail="A turn is already in progress for this session. Please retry.",
        )

    async def event_gen():
        try:
            async for chunk in stream_session_turn(
                session_id=session_id,
                game_session=session,
                body_message=body.message,
                initial_state=initial_state,
                db=db,
            ):
                yield chunk
        finally:
            structlog.contextvars.unbind_contextvars("session_id", "campaign_id")
            await release_turn_lock(session_id, redis)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
