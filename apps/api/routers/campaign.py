from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from db.postgres.models import Campaign
from dependencies import get_session
from schemas.campaign import CampaignCreate, CampaignRead, CampaignUpdate

router = APIRouter(tags=["campaigns"])


@router.post("/campaigns", response_model=CampaignRead)
async def create_campaign(body: CampaignCreate, db: AsyncSession = Depends(get_session)):
    campaign = Campaign(title=body.title, system=body.system, created_by=body.created_by or 1)
    db.add(campaign)
    await db.commit()
    await db.refresh(campaign)
    return CampaignRead.model_validate(campaign)