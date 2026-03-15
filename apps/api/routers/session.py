from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from db.postgres.models import Session as SessionModel
from dependencies import get_session
from schemas.session import SessionCreate, SessionRead

router = APIRouter(tags=["sessions"])


@router.post("/sessions", response_model=SessionRead)
async def create_session(body: SessionCreate, db: AsyncSession = Depends(get_session)):
    session = SessionModel(campaign_id=body.campaign_id, ended_at=body.ended_at)
    if body.started_at is not None:
        session.started_at = body.started_at
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return SessionRead.model_validate(session)
