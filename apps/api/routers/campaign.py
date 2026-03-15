from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.postgres.models import Campaign
from dependencies import get_session
from schemas.campaign import CampaignCreate, CampaignRead, CampaignUpdate

router = APIRouter(tags=["campaigns"])


@router.get("/campaigns", response_model=list[CampaignRead])
async def list_campaigns(db: AsyncSession = Depends(get_session)):
    result = await db.execute(select(Campaign))
    campaigns = result.scalars().all()
    return [CampaignRead.model_validate(c) for c in campaigns]


@router.get("/campaigns/{campaign_id}", response_model=CampaignRead)
async def get_campaign(campaign_id: int, db: AsyncSession = Depends(get_session)):
    result = await db.execute(select(Campaign).where(Campaign.id == campaign_id))
    campaign = result.scalar_one_or_none()
    if campaign is None:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return CampaignRead.model_validate(campaign)


@router.post("/campaigns", response_model=CampaignRead)
async def create_campaign(body: CampaignCreate, db: AsyncSession = Depends(get_session)):
    campaign = Campaign(title=body.title, system=body.system, created_by=body.created_by or 1)
    db.add(campaign)
    await db.commit()
    await db.refresh(campaign)
    return CampaignRead.model_validate(campaign)


@router.patch("/campaigns/{campaign_id}", response_model=CampaignRead)
async def update_campaign(
    campaign_id: int, body: CampaignUpdate, db: AsyncSession = Depends(get_session)
):
    result = await db.execute(select(Campaign).where(Campaign.id == campaign_id))
    campaign = result.scalar_one_or_none()
    if campaign is None:
        raise HTTPException(status_code=404, detail="Campaign not found")
    update_data = body.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(campaign, key, value)
    await db.commit()
    await db.refresh(campaign)
    return CampaignRead.model_validate(campaign)


@router.delete("/campaigns/{campaign_id}", status_code=204)
async def delete_campaign(campaign_id: int, db: AsyncSession = Depends(get_session)):
    result = await db.execute(select(Campaign).where(Campaign.id == campaign_id))
    campaign = result.scalar_one_or_none()
    if campaign is None:
        raise HTTPException(status_code=404, detail="Campaign not found")
    db.delete(campaign)
    await db.commit()
