"""Seed endpoint: create default user, campaign, session for quick start."""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel

from db.postgres import get_session
from db.postgres.models import User, Campaign, Session

router = APIRouter(tags=["seed"])


class SeedResponse(BaseModel):
    """Response from seed endpoint."""

    user_id: int
    campaign_id: int
    session_id: int
    message: str


@router.post("/seed", response_model=SeedResponse)
async def seed(db: AsyncSession = Depends(get_session)):
    """Create default user, campaign, and session. Idempotent: reuses existing if found."""
    result = await db.execute(select(User).where(User.email == "dm@edgar.local"))
    user = result.scalar_one_or_none()
    if user is None:
        user = User(email="dm@edgar.local")
        db.add(user)
        await db.commit()
        await db.refresh(user)

    result = await db.execute(select(Campaign).where(Campaign.created_by == user.id).limit(1))
    campaign = result.scalar_one_or_none()
    if campaign is None:
        campaign = Campaign(title="Solo Session", system="D&D 5e", created_by=user.id)
        db.add(campaign)
        await db.commit()
        await db.refresh(campaign)

    result = await db.execute(select(Session).where(Session.campaign_id == campaign.id).limit(1))
    session = result.scalar_one_or_none()
    if session is None:
        session = Session(campaign_id=campaign.id, started_at=datetime.now(timezone.utc))
        db.add(session)
        await db.commit()
        await db.refresh(session)

    return SeedResponse(
        user_id=user.id,
        campaign_id=campaign.id,
        session_id=session.id,
        message=f"Ready. Use session_id={session.id} for POST /api/sessions/{session.id}/turn",
    )
