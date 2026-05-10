"""World-flag admin CRUD — Phase 9.

World flags are normally set by the adjudicator; these endpoints allow manual
overrides for debugging and DM-style control.
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.postgres.models import Campaign, WorldFlag
from ..dependencies import get_session
from ..schemas.world_flag import WorldFlagCreate, WorldFlagRead, WorldFlagUpdate

router = APIRouter(tags=["world-flags"])


async def _require_campaign(campaign_id: int, db: AsyncSession) -> Campaign:
    result = await db.execute(select(Campaign).where(Campaign.id == campaign_id))
    campaign = result.scalar_one_or_none()
    if campaign is None:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return campaign


@router.get("/campaigns/{campaign_id}/world-flags", response_model=list[WorldFlagRead])
async def list_world_flags(campaign_id: int, db: AsyncSession = Depends(get_session)):
    await _require_campaign(campaign_id, db)
    result = await db.execute(
        select(WorldFlag).where(WorldFlag.campaign_id == campaign_id)
    )
    return [WorldFlagRead.model_validate(f) for f in result.scalars().all()]


@router.post("/campaigns/{campaign_id}/world-flags", response_model=WorldFlagRead, status_code=201)
async def create_or_update_world_flag(
    campaign_id: int, body: WorldFlagCreate, db: AsyncSession = Depends(get_session)
):
    """Upsert: if the key already exists, update the value; otherwise insert."""
    await _require_campaign(campaign_id, db)
    result = await db.execute(
        select(WorldFlag).where(
            WorldFlag.campaign_id == campaign_id, WorldFlag.key == body.key
        )
    )
    flag = result.scalar_one_or_none()
    if flag is None:
        flag = WorldFlag(campaign_id=campaign_id, key=body.key, value=body.value)
        db.add(flag)
    else:
        flag.value = body.value
    await db.commit()
    await db.refresh(flag)
    return WorldFlagRead.model_validate(flag)


@router.patch("/campaigns/{campaign_id}/world-flags/{key}", response_model=WorldFlagRead)
async def update_world_flag(
    campaign_id: int,
    key: str,
    body: WorldFlagUpdate,
    db: AsyncSession = Depends(get_session),
):
    await _require_campaign(campaign_id, db)
    result = await db.execute(
        select(WorldFlag).where(
            WorldFlag.campaign_id == campaign_id, WorldFlag.key == key
        )
    )
    flag = result.scalar_one_or_none()
    if flag is None:
        raise HTTPException(status_code=404, detail="World flag not found")
    flag.value = body.value
    await db.commit()
    await db.refresh(flag)
    return WorldFlagRead.model_validate(flag)


@router.delete("/campaigns/{campaign_id}/world-flags/{key}", status_code=204)
async def delete_world_flag(
    campaign_id: int, key: str, db: AsyncSession = Depends(get_session)
):
    await _require_campaign(campaign_id, db)
    result = await db.execute(
        select(WorldFlag).where(
            WorldFlag.campaign_id == campaign_id, WorldFlag.key == key
        )
    )
    flag = result.scalar_one_or_none()
    if flag is None:
        raise HTTPException(status_code=404, detail="World flag not found")
    await db.delete(flag)
    await db.commit()
