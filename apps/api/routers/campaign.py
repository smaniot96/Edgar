from typing import Literal

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from redis.asyncio import Redis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from db.postgres.models import Campaign, CharacterAssignment
from db.vector.collections import DEFAULT_ADVENTURE_COLLECTIONS

from ..auth import Auth, campaign_scope, owned_campaign, require_admin
from ..dependencies import current_user_id, get_redis, get_session
from ..limits import admit_job
from ..schemas.campaign import CampaignCreate, CampaignRead, CampaignUpdate
from ..services.campaign_completion import close_campaign, lock_campaign
from ..services.campaign_sources import campaign_read, require_sources, validate_binding
from ._common import Page

router = APIRouter(tags=["campaigns"])


@router.get("/campaigns", response_model=list[CampaignRead])
async def list_campaigns(
    auth: Auth,
    db: AsyncSession = Depends(get_session),
    status: Literal["active", "ended"] | None = Query(
        default=None,
        description="Filter by lifecycle status (omit for all campaigns)",
    ),
    page: Page = Depends(),
):
    stmt = select(Campaign).order_by(Campaign.id).limit(page.limit).offset(page.offset)
    scope = campaign_scope(auth)
    if scope is not None:
        stmt = stmt.where(scope)
    if status is not None:
        stmt = stmt.where(Campaign.status == status)
    result = await db.execute(stmt)
    campaigns = result.scalars().all()
    return [await campaign_read(c) for c in campaigns]


@router.get("/campaigns/{campaign_id}", response_model=CampaignRead)
async def get_campaign(campaign_id: int, auth: Auth, db: AsyncSession = Depends(get_session)):
    campaign = await owned_campaign(db, campaign_id, auth)
    return await campaign_read(campaign)


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
        return await campaign_read(existing)

    await require_sources([slug], "module")
    meta = await run_in_threadpool(load_adventure_metadata, slug)
    title = meta.get("title") or slug.replace("_", " ").title()
    campaign = Campaign(
        title=title,
        rules_edition=meta.get("rules_edition", "2014"),
        system="D&D 5e",
        created_by=owner_id,
        adventure_collections=[slug],
    )
    db.add(campaign)
    await db.commit()
    await db.refresh(campaign)
    return await campaign_read(campaign)


@router.post("/campaigns", response_model=CampaignRead, status_code=201)
async def create_campaign(
    body: CampaignCreate,
    db: AsyncSession = Depends(get_session),
    owner_id: int = Depends(current_user_id),
):
    adv = (
        body.adventure_collections
        if body.adventure_collections is not None
        else ([] if body.source_mode == "homebrew" else list(DEFAULT_ADVENTURE_COLLECTIONS))
    )
    for slug in adv:
        validate_binding(slug)
    await require_sources(adv, body.source_mode)
    from ..services.prepared_play import require_prepared_play

    await run_in_threadpool(
        require_prepared_play,
        [] if body.source_mode == "homebrew" else adv,
        body.rules_edition,
    )
    campaign = Campaign(
        source_mode=body.source_mode,
        rules_edition=body.rules_edition,
        title=body.title,
        system=body.system,
        created_by=owner_id,
        adventure_collections=adv,
    )
    db.add(campaign)
    await db.commit()
    await db.refresh(campaign)
    return await campaign_read(campaign)


@router.patch("/campaigns/{campaign_id}", response_model=CampaignRead)
async def update_campaign(
    campaign_id: int, body: CampaignUpdate, auth: Auth, db: AsyncSession = Depends(get_session)
):
    campaign = await owned_campaign(db, campaign_id, auth)
    update_data = body.model_dump(exclude_unset=True)
    from ..services.prepared_play import protect_prepared_campaign_binding, require_prepared_play

    await run_in_threadpool(protect_prepared_campaign_binding, campaign, update_data)
    for slug in update_data.get("adventure_collections", []):
        validate_binding(slug)
    if "adventure_collections" in update_data or "source_mode" in update_data:
        await require_sources(
            update_data.get("adventure_collections", campaign.adventure_collections),
            update_data.get("source_mode", campaign.source_mode),
        )
    await run_in_threadpool(
        require_prepared_play,
        []
        if update_data.get("source_mode", campaign.source_mode) == "homebrew"
        else update_data.get("adventure_collections", campaign.adventure_collections),
        update_data.get("rules_edition", campaign.rules_edition),
    )
    for key, value in update_data.items():
        setattr(campaign, key, value)
    await db.commit()
    await db.refresh(campaign)
    return await campaign_read(campaign)


@router.delete("/campaigns/{campaign_id}", status_code=204)
async def delete_campaign(campaign_id: int, auth: Auth, db: AsyncSession = Depends(get_session)):
    campaign = await owned_campaign(db, campaign_id, auth)
    await db.delete(campaign)
    await db.commit()


@router.post("/campaigns/{campaign_id}/end", response_model=CampaignRead)
async def end_campaign(campaign_id: int, auth: Auth, db: AsyncSession = Depends(get_session)):
    """Mark the campaign as ended and close active character assignments (plan 03).

    The closed assignments get exactly `campaign.ended_at` as their `ended_at`; that shared
    timestamp is what `reopen` uses to find (only) the assignments this end closed.
    """
    campaign = await owned_campaign(db, campaign_id, auth)
    if not await close_campaign(db, campaign):
        return await campaign_read(campaign)
    await db.commit()
    await db.refresh(campaign)
    return await campaign_read(campaign)


@router.post("/campaigns/{campaign_id}/reopen", response_model=CampaignRead)
async def reopen_campaign(campaign_id: int, auth: Auth, db: AsyncSession = Depends(get_session)):
    """Undo a premature end; restores the assignments closed by that end.

    Deterministic rule: `end` stamps the campaign and every assignment it closes with the same
    timestamp, so exactly the assignments whose `ended_at` equals `campaign.ended_at` are
    reopened. Assignments released manually earlier keep their own (different) `ended_at`.
    A character that has since become active in another campaign is left ended (a character
    can only be active in one campaign at a time).
    """
    campaign = await owned_campaign(db, campaign_id, auth)
    await lock_campaign(db, campaign)
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
    return await campaign_read(campaign)


@router.post(
    "/campaigns/{campaign_id}/sources/{slug}/upload",
    status_code=202,
    # Recovery rewrites a shared source-library file, so it is an operator action as well as
    # an ownership check on the campaign that is bound to it.
    dependencies=[Depends(require_admin)],
)
async def recover_campaign_source(
    campaign_id: int,
    slug: str,
    background: BackgroundTasks,
    auth: Auth,
    request: Request,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
):
    """Recover an unusable bound source at its original slug using the player's PDF.

    Bindings and session history stay intact. This does not replace a ready source and never
    synthesizes a lost PDF from old logs or metadata. Ingestion uses the existing pipeline.
    Rate and job-concurrency limits apply (EDG-066).
    """
    from .adventures import recover_source_upload

    admission = await admit_job(request, redis)
    try:
        campaign = await owned_campaign(db, campaign_id, auth)
        if slug not in campaign.adventure_collections:
            raise HTTPException(
                status_code=422, detail="That source is not bound to this campaign."
            )
        return await recover_source_upload(slug, file, background, admission)
    finally:
        await admission.settle()
