from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from db.postgres.models import Campaign, CharacterAssignment
from db.vector.collections import DEFAULT_ADVENTURE_COLLECTIONS
from ..dependencies import current_user_id, get_session
from ..schemas.campaign import CampaignCreate, CampaignRead, CampaignUpdate

router = APIRouter(tags=["campaigns"])


async def _load_campaign_or_404(db: AsyncSession, campaign_id: int) -> Campaign:
    result = await db.execute(select(Campaign).where(Campaign.id == campaign_id))
    campaign = result.scalar_one_or_none()
    if campaign is None:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return campaign


@router.get("/campaigns", response_model=list[CampaignRead])
async def list_campaigns(
    db: AsyncSession = Depends(get_session),
    status: Literal["active", "ended"] | None = Query(
        default=None,
        description="Filter by lifecycle status (omit for all campaigns)",
    ),
):
    stmt = select(Campaign)
    if status is not None:
        stmt = stmt.where(Campaign.status == status)
    result = await db.execute(stmt)
    campaigns = result.scalars().all()
    return [CampaignRead.model_validate(c) for c in campaigns]


@router.get("/campaigns/{campaign_id}", response_model=CampaignRead)
async def get_campaign(campaign_id: int, db: AsyncSession = Depends(get_session)):
    campaign = await _load_campaign_or_404(db, campaign_id)
    return CampaignRead.model_validate(campaign)


@router.post("/campaigns", response_model=CampaignRead)
async def create_campaign(
    body: CampaignCreate,
    db: AsyncSession = Depends(get_session),
    owner_id: int = Depends(current_user_id),
):
    adv = (
        body.adventure_collections
        if body.adventure_collections is not None
        else list(DEFAULT_ADVENTURE_COLLECTIONS)
    )
    created_by = body.created_by if body.created_by is not None else owner_id
    campaign = Campaign(
        title=body.title,
        system=body.system,
        created_by=created_by,
        adventure_collections=adv,
    )
    db.add(campaign)
    await db.commit()
    await db.refresh(campaign)
    return CampaignRead.model_validate(campaign)


@router.patch("/campaigns/{campaign_id}", response_model=CampaignRead)
async def update_campaign(
    campaign_id: int, body: CampaignUpdate, db: AsyncSession = Depends(get_session)
):
    campaign = await _load_campaign_or_404(db, campaign_id)
    update_data = body.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(campaign, key, value)
    await db.commit()
    await db.refresh(campaign)
    return CampaignRead.model_validate(campaign)


@router.delete("/campaigns/{campaign_id}", status_code=204)
async def delete_campaign(campaign_id: int, db: AsyncSession = Depends(get_session)):
    campaign = await _load_campaign_or_404(db, campaign_id)
    await db.delete(campaign)
    await db.commit()


@router.post("/campaigns/{campaign_id}/end", response_model=CampaignRead)
async def end_campaign(campaign_id: int, db: AsyncSession = Depends(get_session)):
    """Mark the campaign as ended and close active character assignments (plan 03)."""
    campaign = await _load_campaign_or_404(db, campaign_id)
    if campaign.status == "ended":
        return CampaignRead.model_validate(campaign)
    now = datetime.now(timezone.utc)
    campaign.status = "ended"
    campaign.ended_at = now
    await db.execute(
        update(CharacterAssignment)
        .where(
            CharacterAssignment.campaign_id == campaign_id,
            CharacterAssignment.ended_at.is_(None),
        )
        .values(ended_at=now)
    )
    await db.commit()
    await db.refresh(campaign)
    return CampaignRead.model_validate(campaign)


@router.post("/campaigns/{campaign_id}/reopen", response_model=CampaignRead)
async def reopen_campaign(campaign_id: int, db: AsyncSession = Depends(get_session)):
    """Undo a premature end; restores assignments ended together with this campaign end."""
    campaign = await _load_campaign_or_404(db, campaign_id)
    ended_snapshot = campaign.ended_at
    campaign.status = "active"
    campaign.ended_at = None
    if ended_snapshot is not None:
        low = ended_snapshot - timedelta(seconds=2)
        high = ended_snapshot + timedelta(seconds=2)
        await db.execute(
            update(CharacterAssignment)
            .where(
                CharacterAssignment.campaign_id == campaign_id,
                CharacterAssignment.ended_at >= low,
                CharacterAssignment.ended_at <= high,
            )
            .values(ended_at=None)
        )
    await db.commit()
    await db.refresh(campaign)
    return CampaignRead.model_validate(campaign)
