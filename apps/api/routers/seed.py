"""Idempotent bootstrap for solo play: ensures a user, campaign, session, and character exist.

Safe to call repeatedly; existing rows are reused. The chat UI calls this on first load so a
fresh database is playable immediately without any manual setup.

The user is the *current* user (the first user, same rule as `current_user_id`): if the setup
screen already created one, seed attaches everything to it instead of adding a second
`dm@edgar.local` user, so the current user never flips.
"""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.postgres.models import Campaign, Character, CharacterAssignment, Session, User
from db.vector.collections import DEFAULT_ADVENTURE_COLLECTIONS

from ..dependencies import get_session
from ..services.character_assignments import seed_assignment_row_payload

router = APIRouter(tags=["seed"])

_SEED_EMAIL = "dm@edgar.local"


class SeedResponse(BaseModel):
    user_id: int
    campaign_id: int
    session_id: int
    message: str


@router.post("/seed", response_model=SeedResponse)
async def seed(db: AsyncSession = Depends(get_session)):
    result = await db.execute(select(User).order_by(User.id).limit(1))
    user = result.scalar_one_or_none()
    if user is None:
        user = User(email=_SEED_EMAIL)
        db.add(user)
        await db.commit()
        await db.refresh(user)

    result = await db.execute(select(Campaign).where(Campaign.created_by == user.id).order_by(Campaign.id).limit(1))
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

    result = await db.execute(select(Session).where(Session.campaign_id == campaign.id).order_by(Session.id).limit(1))
    session = result.scalar_one_or_none()
    if session is None:
        session = Session(campaign_id=campaign.id, started_at=datetime.now(UTC))
        db.add(session)
        await db.commit()
        await db.refresh(session)

    if session.active_character_id is None:
        char = Character(
            owner_user_id=user.id,
            name="Eda",
            character_class="Fighter",
            level=1,
            hp_max=12,
            base_stats={"STR": 16, "DEX": 12, "CON": 14, "INT": 10, "WIS": 12, "CHA": 8},
            base_inventory={"weapons": ["longsword"], "armor": "chain shirt"},
        )
        db.add(char)
        await db.commit()
        await db.refresh(char)

        payload = seed_assignment_row_payload(char)
        assignment = CharacterAssignment(
            character_id=char.id,
            campaign_id=campaign.id,
            **payload,
        )
        db.add(assignment)
        session.active_character_id = char.id
        await db.commit()
        await db.refresh(session)

    return SeedResponse(
        user_id=user.id,
        campaign_id=campaign.id,
        session_id=session.id,
        message=f"Ready. Use session_id={session.id} for POST /api/sessions/{session.id}/turn",
    )
