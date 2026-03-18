import asyncio

from fastapi import APIRouter, Depends, HTTPException
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.postgres.models import Session as SessionModel
from dependencies import get_session, get_redis, acquire_turn_lock, release_turn_lock
from schemas.session import SessionCreate, SessionRead, SessionUpdate
from schemas.turn import TurnRequest, TurnResponse

router = APIRouter(tags=["sessions"])


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
    db.delete(session)
    await db.commit()


@router.post("/sessions/{session_id}/turn", response_model=TurnResponse)
async def session_turn(
    session_id: int,
    body: TurnRequest,
    db: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
):
    """Process a player turn: acquire lock, invoke agent, return narration."""
    result = await db.execute(select(SessionModel).where(SessionModel.id == session_id))
    session = result.scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    acquired = await acquire_turn_lock(session_id, redis)
    if not acquired:
        raise HTTPException(
            status_code=409,
            detail="A turn is already in progress for this session. Please retry.",
        )

    try:
        from agent.graph import app

        initial_state = {
            "player_input": body.message,
            "session_id": session_id,
            "campaign_id": session.campaign_id,
            "messages": [],
        }
        result = await asyncio.to_thread(app.invoke, initial_state)

        if result.get("error"):
            raise HTTPException(status_code=503, detail=result["error"])

        adjudication = result.get("adjudication_result")
        return TurnResponse(
            narration=result.get("narration", ""),
            adjudication=adjudication.model_dump() if adjudication else None,
            combat_state=result.get("combat_state"),
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        await release_turn_lock(session_id, redis)
