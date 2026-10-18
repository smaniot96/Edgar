from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from agent.progression import ProgressionError
from agent.resolution import ENGINE_STATS_KEYS
from db.postgres.models import Campaign, Character, CharacterAssignment

from ..auth import Auth, authorize_campaign, owned_campaign
from ..dependencies import current_user_id, get_session
from ..schemas.character import (
    CampaignCharacterAssignBody,
    CampaignCharacterPatch,
    CampaignCharacterRead,
)
from ..schemas.progression import LevelUpRead, LevelUpRequest
from ..services.advancement import LevelUpConflict, apply_level_up
from ..services.campaign_completion import lock_campaign
from ..services.character_assignments import (
    load_active_assignment,
    load_active_assignment_any_campaign,
    seed_assignment_row_payload,
)
from ._common import HTTPErrorWithContext, Page

router = APIRouter(tags=["campaigns"])

_ALREADY_ACTIVE = "Character already has an active assignment in another campaign"


def _roster_row(assignment: CharacterAssignment, ch: Character) -> CampaignCharacterRead:
    return CampaignCharacterRead(
        assignment_id=assignment.id,
        character_id=ch.id,
        name=ch.name,
        character_class=ch.character_class,
        level=assignment.level,
        hp_current=assignment.hp_current,
        hp_max=assignment.hp_max,
        milestones_pending=assignment.milestones_pending,
    )


@router.get("/campaigns/{campaign_id}/characters", response_model=list[CampaignCharacterRead])
async def list_campaign_characters(
    campaign_id: int,
    auth: Auth,
    db: AsyncSession = Depends(get_session),
    page: Page = Depends(),
):
    await owned_campaign(db, campaign_id, auth)
    r = await db.execute(
        select(CharacterAssignment)
        .where(
            CharacterAssignment.campaign_id == campaign_id,
            CharacterAssignment.ended_at.is_(None),
        )
        .options(selectinload(CharacterAssignment.character))
        .order_by(CharacterAssignment.id)
        .limit(page.limit)
        .offset(page.offset)
    )
    return [_roster_row(a, a.character) for a in r.scalars().all()]


@router.post(
    "/campaigns/{campaign_id}/characters", response_model=CampaignCharacterRead, status_code=201
)
async def assign_character_to_campaign(
    campaign_id: int,
    body: CampaignCharacterAssignBody,
    auth: Auth,
    db: AsyncSession = Depends(get_session),
    owner_id: int = Depends(current_user_id),
):
    campaign = await owned_campaign(db, campaign_id, auth)
    await lock_campaign(db, campaign)
    if campaign.status == "ended":
        raise HTTPException(
            status_code=409,
            detail="Cannot assign a character to an ended campaign",
        )

    cr = await db.execute(select(Character).where(Character.id == body.character_id))
    character = cr.scalar_one_or_none()
    if character is None:
        raise HTTPException(status_code=404, detail="Character not found")
    if character.owner_user_id != owner_id:
        raise HTTPException(status_code=404, detail="Character not found")

    existing_here = await load_active_assignment(db, body.character_id, campaign_id)
    if existing_here is not None:
        return _roster_row(existing_here, character)

    from starlette.concurrency import run_in_threadpool

    from ..services.prepared_play import require_prepared_play

    await run_in_threadpool(
        require_prepared_play,
        campaign.adventure_collections,
        campaign.rules_edition,
        character=character,
    )

    other_active = await load_active_assignment_any_campaign(db, body.character_id)
    if other_active is not None:
        raise HTTPErrorWithContext(
            409, _ALREADY_ACTIVE, active_campaign_id=other_active.campaign_id
        )

    payload = seed_assignment_row_payload(character)
    assignment = CharacterAssignment(
        character_id=character.id,
        campaign_id=campaign_id,
        **payload,
    )
    db.add(assignment)
    try:
        await db.commit()
    except IntegrityError as exc:
        # Lost a race with a concurrent assign: the partial unique index
        # `character_assignments_one_active` allows one active assignment per character.
        await db.rollback()
        if "character_assignments_one_active" not in str(exc.orig):
            raise
        raise HTTPException(status_code=409, detail=_ALREADY_ACTIVE) from exc
    await db.refresh(assignment)
    return _roster_row(assignment, character)


@router.patch(
    "/campaigns/{campaign_id}/characters/{character_id}",
    response_model=CampaignCharacterRead,
)
async def patch_campaign_character(
    campaign_id: int,
    character_id: int,
    body: CampaignCharacterPatch,
    auth: Auth,
    db: AsyncSession = Depends(get_session),
    owner_id: int = Depends(current_user_id),
):
    await authorize_campaign(db, campaign_id, auth)
    cr = await db.execute(select(Character).where(Character.id == character_id))
    character = cr.scalar_one_or_none()
    if character is None:
        raise HTTPException(status_code=404, detail="Character not found")
    if character.owner_user_id != owner_id:
        raise HTTPException(status_code=404, detail="Character not found")

    assignment = await load_active_assignment(db, character_id, campaign_id)
    if assignment is None:
        raise HTTPException(
            status_code=404, detail="No active assignment for this character in this campaign"
        )

    data = body.model_dump(exclude_unset=True)
    new_hp_current = data.get("hp_current", assignment.hp_current)
    new_hp_max = data.get("hp_max", assignment.hp_max)
    if new_hp_current > new_hp_max:
        raise HTTPException(
            status_code=422,
            detail=f"hp_current ({new_hp_current}) cannot exceed hp_max ({new_hp_max})",
        )
    if "stats" in data and isinstance(assignment.stats, dict):
        # A manual sheet edit must not reset engine-owned resources (Second Wind uses, spell slots).
        kept = {k: assignment.stats[k] for k in ENGINE_STATS_KEYS if k in assignment.stats}
        data["stats"] = {**data["stats"], **kept}
    for key, value in data.items():
        setattr(assignment, key, value)

    await db.commit()
    await db.refresh(assignment)
    return _roster_row(assignment, character)


@router.post(
    "/campaigns/{campaign_id}/characters/{character_id}/level-up",
    response_model=LevelUpRead,
)
async def level_up_campaign_character(
    campaign_id: int,
    character_id: int,
    body: LevelUpRequest,
    auth: Auth,
    db: AsyncSession = Depends(get_session),
    owner_id: int = Depends(current_user_id),
):
    """Spend one milestone on one level (EDG-045). Atomic; a repeated request_id replays the result."""
    await authorize_campaign(db, campaign_id, auth)
    cr = await db.execute(select(Character).where(Character.id == character_id))
    character = cr.scalar_one_or_none()
    if character is None or character.owner_user_id != owner_id:
        raise HTTPException(status_code=404, detail="Character not found")
    campaign = await owned_campaign(db, campaign_id, auth)
    await lock_campaign(db, campaign)
    try:
        result = await apply_level_up(
            db,
            campaign,
            character_id,
            from_level=body.from_level,
            request_id=body.request_id,
            hp_method=body.hp_method,
            ability_increases=body.ability_increases,
        )
    except LevelUpConflict as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ProgressionError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if result.applied:
        await db.commit()
    assignment = await load_active_assignment(db, character_id, campaign_id)
    record = result.record
    return LevelUpRead(
        applied=result.applied,
        from_level=record["from_level"],
        to_level=record["to_level"],
        hit_points=record["hit_points"],
        ability_increases=record.get("ability_increases") or {},
        features=record.get("features") or [],
        spell_slots=record.get("spell_slots") or {},
        milestones_pending=record["milestones_pending"],
        character=_roster_row(assignment, character),
    )


@router.delete("/campaigns/{campaign_id}/characters/{character_id}", status_code=204)
async def release_character_from_campaign(
    campaign_id: int,
    character_id: int,
    auth: Auth,
    db: AsyncSession = Depends(get_session),
    owner_id: int = Depends(current_user_id),
):
    await authorize_campaign(db, campaign_id, auth)
    cr = await db.execute(select(Character).where(Character.id == character_id))
    character = cr.scalar_one_or_none()
    if character is None:
        raise HTTPException(status_code=404, detail="Character not found")
    if character.owner_user_id != owner_id:
        raise HTTPException(status_code=404, detail="Character not found")

    # Lock the campaign before reading the assignment so a concurrent end cannot be overwritten.
    await db.execute(select(Campaign.id).where(Campaign.id == campaign_id).with_for_update())
    assignment = await load_active_assignment(db, character_id, campaign_id)
    if assignment is None:
        raise HTTPException(
            status_code=404, detail="No active assignment for this character in this campaign"
        )

    assignment.ended_at = datetime.now(UTC)
    await db.commit()
    return None
