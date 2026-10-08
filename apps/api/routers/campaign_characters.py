from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from db.postgres.models import Campaign, Character, CharacterAssignment

from ..dependencies import current_user_id, get_session
from ..schemas.character import (
    CampaignCharacterAssignBody,
    CampaignCharacterPatch,
    CampaignCharacterRead,
)
from ..services.character_assignments import (
    load_active_assignment,
    load_active_assignment_any_campaign,
    seed_assignment_row_payload,
)
from ._common import HTTPErrorWithContext, Page, get_or_404

router = APIRouter(tags=["campaigns"])

_ALREADY_ACTIVE = "Character already has an active assignment in another campaign"


async def _campaign_or_404(db: AsyncSession, campaign_id: int) -> Campaign:
    return await get_or_404(db, Campaign, campaign_id, "Campaign")


def _roster_row(assignment: CharacterAssignment, ch: Character) -> CampaignCharacterRead:
    return CampaignCharacterRead(
        assignment_id=assignment.id,
        character_id=ch.id,
        name=ch.name,
        character_class=ch.character_class,
        level=ch.level,
        hp_current=assignment.hp_current,
        hp_max=assignment.hp_max,
    )


@router.get("/campaigns/{campaign_id}/characters", response_model=list[CampaignCharacterRead])
async def list_campaign_characters(
    campaign_id: int,
    db: AsyncSession = Depends(get_session),
    page: Page = Depends(),
):
    await _campaign_or_404(db, campaign_id)
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


@router.post("/campaigns/{campaign_id}/characters", response_model=CampaignCharacterRead, status_code=201)
async def assign_character_to_campaign(
    campaign_id: int,
    body: CampaignCharacterAssignBody,
    db: AsyncSession = Depends(get_session),
    owner_id: int = Depends(current_user_id),
):
    campaign = await _campaign_or_404(db, campaign_id)
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
        raise HTTPException(status_code=403, detail="Character does not belong to the current user")

    existing_here = await load_active_assignment(db, body.character_id, campaign_id)
    if existing_here is not None:
        return _roster_row(existing_here, character)

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
    db: AsyncSession = Depends(get_session),
    owner_id: int = Depends(current_user_id),
):
    cr = await db.execute(select(Character).where(Character.id == character_id))
    character = cr.scalar_one_or_none()
    if character is None:
        raise HTTPException(status_code=404, detail="Character not found")
    if character.owner_user_id != owner_id:
        raise HTTPException(status_code=403, detail="Character does not belong to the current user")

    assignment = await load_active_assignment(db, character_id, campaign_id)
    if assignment is None:
        raise HTTPException(status_code=404, detail="No active assignment for this character in this campaign")

    data = body.model_dump(exclude_unset=True)
    new_hp_current = data.get("hp_current", assignment.hp_current)
    new_hp_max = data.get("hp_max", assignment.hp_max)
    if new_hp_current > new_hp_max:
        raise HTTPException(
            status_code=422,
            detail=f"hp_current ({new_hp_current}) cannot exceed hp_max ({new_hp_max})",
        )
    for key, value in data.items():
        setattr(assignment, key, value)

    await db.commit()
    await db.refresh(assignment)
    return _roster_row(assignment, character)


@router.delete("/campaigns/{campaign_id}/characters/{character_id}", status_code=204)
async def release_character_from_campaign(
    campaign_id: int,
    character_id: int,
    db: AsyncSession = Depends(get_session),
    owner_id: int = Depends(current_user_id),
):
    cr = await db.execute(select(Character).where(Character.id == character_id))
    character = cr.scalar_one_or_none()
    if character is None:
        raise HTTPException(status_code=404, detail="Character not found")
    if character.owner_user_id != owner_id:
        raise HTTPException(status_code=403, detail="Character does not belong to the current user")

    assignment = await load_active_assignment(db, character_id, campaign_id)
    if assignment is None:
        raise HTTPException(status_code=404, detail="No active assignment for this character in this campaign")

    assignment.ended_at = datetime.now(UTC)
    await db.commit()
    return None
