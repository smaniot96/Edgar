"""Idempotent bootstrap for solo play: ensures a user, campaign, session, and character exist.

Safe to call repeatedly; existing rows are reused. The chat UI calls this on first load so a
fresh database is playable immediately without any manual setup.
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.postgres.models import Campaign, Character, Session, User
from db.vector.collections import DEFAULT_ADVENTURE_COLLECTIONS

from ..dependencies import get_session

router = APIRouter(tags=["seed"])


class SeedResponse(BaseModel):
    user_id: int
    campaign_id: int
    session_id: int
    message: str


@router.post("/seed", response_model=SeedResponse)
async def seed(db: AsyncSession = Depends(get_session)):
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
        campaign = Campaign(
            title="Solo Session",
            system="D&D 5e",
            created_by=user.id,
            adventure_collections=list(DEFAULT_ADVENTURE_COLLECTIONS),
        )
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

    if session.active_character_id is None:
        char = Character(
            campaign_id=campaign.id,
            name="Eda",
            character_class="Fighter",
            level=1,
            hp_current=12,
            hp_max=12,
            stats={"STR": 16, "DEX": 12, "CON": 14, "INT": 10, "WIS": 12, "CHA": 8},
            inventory={"weapons": ["longsword"], "armor": "chain shirt"},
        )
        db.add(char)
        await db.commit()
        await db.refresh(char)
        session.active_character_id = char.id
        await db.commit()
        await db.refresh(session)

    return SeedResponse(
        user_id=user.id,
        campaign_id=campaign.id,
        session_id=session.id,
        message=f"Ready. Use session_id={session.id} for POST /api/sessions/{session.id}/turn",
    )
