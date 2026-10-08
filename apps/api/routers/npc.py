"""NPC CRUD router — Phase 8.

All write endpoints are scoped to a campaign; reads can be done by NPC id directly.
"""

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.postgres.models import NPC, Campaign

from ..dependencies import get_session
from ..schemas.npc import NPCCreate, NPCRead, NPCUpdate
from ._common import Page, get_or_404

router = APIRouter(tags=["npcs"])


@router.get("/campaigns/{campaign_id}/npcs", response_model=list[NPCRead])
async def list_npcs(
    campaign_id: int, db: AsyncSession = Depends(get_session), page: Page = Depends()
):
    await get_or_404(db, Campaign, campaign_id, "Campaign")
    result = await db.execute(
        select(NPC)
        .where(NPC.campaign_id == campaign_id)
        .order_by(NPC.id)
        .limit(page.limit)
        .offset(page.offset)
    )
    return [NPCRead.model_validate(n) for n in result.scalars().all()]


@router.post("/campaigns/{campaign_id}/npcs", response_model=NPCRead, status_code=201)
async def create_npc(
    campaign_id: int, body: NPCCreate, db: AsyncSession = Depends(get_session)
):
    await get_or_404(db, Campaign, campaign_id, "Campaign")
    npc = NPC(
        campaign_id=campaign_id,
        name=body.name,
        disposition=body.disposition,
        stat_block=body.stat_block,
    )
    db.add(npc)
    await db.commit()
    await db.refresh(npc)
    return NPCRead.model_validate(npc)


@router.get("/npcs/{npc_id}", response_model=NPCRead)
async def get_npc(npc_id: int, db: AsyncSession = Depends(get_session)):
    npc = await get_or_404(db, NPC, npc_id, "NPC")
    return NPCRead.model_validate(npc)


@router.patch("/npcs/{npc_id}", response_model=NPCRead)
async def update_npc(
    npc_id: int, body: NPCUpdate, db: AsyncSession = Depends(get_session)
):
    npc = await get_or_404(db, NPC, npc_id, "NPC")
    for key, value in body.model_dump(exclude_unset=True).items():
        setattr(npc, key, value)
    await db.commit()
    await db.refresh(npc)
    return NPCRead.model_validate(npc)


@router.delete("/npcs/{npc_id}", status_code=204)
async def delete_npc(npc_id: int, db: AsyncSession = Depends(get_session)):
    npc = await get_or_404(db, NPC, npc_id, "NPC")
    await db.delete(npc)
    await db.commit()
