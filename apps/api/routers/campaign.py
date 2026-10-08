from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from db.postgres.models import Campaign, CharacterAssignment
from db.vector.collections import DEFAULT_ADVENTURE_COLLECTIONS

from ..dependencies import current_user_id, get_session
from ..schemas.campaign import CampaignCreate, CampaignRead, CampaignUpdate
from ._common import Page, get_or_404

router = APIRouter(tags=["campaigns"])


async def _load_campaign_or_404(db: AsyncSession, campaign_id: int) -> Campaign:
    return await get_or_404(db, Campaign, campaign_id, "Campaign")


@router.get("/campaigns", response_model=list[CampaignRead])
async def list_campaigns(
    db: AsyncSession = Depends(get_session),
    status: Literal["active", "ended"] | None = Query(
        default=None,
        description="Filter by lifecycle status (omit for all campaigns)",
    ),
    page: Page = Depends(),
):
    stmt = select(Campaign).order_by(Campaign.id).limit(page.limit).offset(page.offset)
    if status is not None:
        stmt = stmt.where(Campaign.status == status)
    result = await db.execute(stmt)
    campaigns = result.scalars().all()
    return [CampaignRead.model_validate(c) for c in campaigns]


@router.get("/campaigns/{campaign_id}", response_model=CampaignRead)
async def get_campaign(campaign_id: int, db: AsyncSession = Depends(get_session)):
    campaign = await _load_campaign_or_404(db, campaign_id)
    return CampaignRead.model_validate(campaign)


@router.post("/adventures/{slug}/campaign", response_model=CampaignRead)
async def open_campaign_for_adventure(
    slug: str,
    db: AsyncSession = Depends(get_session),
    owner_id: int = Depends(current_user_id),
):
    """Get-or-create the playable campaign for an uploaded adventure module.

    The 'campaigns' the player browses are the uploaded modules; opening one lands on the
    campaign that owns its sessions. We reuse an existing active campaign for this
    (user, adventure) pair so a module maps to one ongoing campaign rather than spawning a new
    one on every click; a fresh one is created on first open (or after the previous ended).
    """
    from .adventures import load_adventure_metadata

    result = await db.execute(
        select(Campaign)
        .where(
            Campaign.created_by == owner_id,
            Campaign.adventure_collections.any(slug),
            Campaign.status == "active",
        )
        .order_by(Campaign.created_at.desc())
        .limit(1)
    )
    existing = result.scalar_one_or_none()
    if existing is not None:
        return CampaignRead.model_validate(existing)

    meta = await run_in_threadpool(load_adventure_metadata, slug)
    title = meta.get("title") or slug.replace("_", " ").title()
    campaign = Campaign(
        title=title,
        system="D&D 5e",
        created_by=owner_id,
        adventure_collections=[slug],
    )
    db.add(campaign)
    await db.commit()
    await db.refresh(campaign)
    return CampaignRead.model_validate(campaign)


@router.post("/campaigns", response_model=CampaignRead, status_code=201)
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
    campaign = Campaign(
        title=body.title,
        system=body.system,
        created_by=owner_id,
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
    """Mark the campaign as ended and close active character assignments (plan 03).

    The closed assignments get exactly `campaign.ended_at` as their `ended_at`; that shared
    timestamp is what `reopen` uses to find (only) the assignments this end closed.
    """
    campaign = await _load_campaign_or_404(db, campaign_id)
    if campaign.status == "ended":
        return CampaignRead.model_validate(campaign)
    now = datetime.now(UTC)
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
    """Undo a premature end; restores the assignments closed by that end.

    Deterministic rule: `end` stamps the campaign and every assignment it closes with the same
    timestamp, so exactly the assignments whose `ended_at` equals `campaign.ended_at` are
    reopened. Assignments released manually earlier keep their own (different) `ended_at`.
    A character that has since become active in another campaign is left ended (a character
    can only be active in one campaign at a time).
    """
    campaign = await _load_campaign_or_404(db, campaign_id)
    ended_snapshot = campaign.ended_at
    campaign.status = "active"
    campaign.ended_at = None
    if ended_snapshot is not None:
        active_elsewhere = select(CharacterAssignment.character_id).where(
            CharacterAssignment.ended_at.is_(None)
        )
        await db.execute(
            update(CharacterAssignment)
            .where(
                CharacterAssignment.campaign_id == campaign_id,
                CharacterAssignment.ended_at == ended_snapshot,
                CharacterAssignment.character_id.not_in(active_elsewhere),
            )
            .values(ended_at=None)
        )
    await db.commit()
    await db.refresh(campaign)
    return CampaignRead.model_validate(campaign)
