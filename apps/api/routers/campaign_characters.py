from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
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

router = APIRouter(tags=["campaigns"])


async def _campaign_or_404(db: AsyncSession, campaign_id: int) -> Campaign:
    r = await db.execute(select(Campaign).where(Campaign.id == campaign_id))
    c = r.scalar_one_or_none()
    if c is None:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return c


@router.get("/campaigns/{campaign_id}/characters", response_model=list[CampaignCharacterRead])
async def list_campaign_characters(
    campaign_id: int,
    db: AsyncSession = Depends(get_session),
):
    await _campaign_or_404(db, campaign_id)
    r = await db.execute(
        select(CharacterAssignment)
        .where(
            CharacterAssignment.campaign_id == campaign_id,
            CharacterAssignment.ended_at.is_(None),
        )
        .options(selectinload(CharacterAssignment.character))
    )
    rows = r.scalars().all()
    out: list[CampaignCharacterRead] = []
    for a in rows:
        ch = a.character
        out.append(
            CampaignCharacterRead(
                assignment_id=a.id,
                character_id=ch.id,
                name=ch.name,
                character_class=ch.character_class,
                level=ch.level,
                hp_current=a.hp_current,
                hp_max=a.hp_max,
            )
        )
    return out


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
        ch = character
        return CampaignCharacterRead(
            assignment_id=existing_here.id,
            character_id=ch.id,
            name=ch.name,
            character_class=ch.character_class,
            level=ch.level,
            hp_current=existing_here.hp_current,
            hp_max=existing_here.hp_max,
        )

    other_active = await load_active_assignment_any_campaign(db, body.character_id)
    if other_active is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Character already has an active assignment in another campaign",
                "active_campaign_id": other_active.campaign_id,
            },
        )

    payload = seed_assignment_row_payload(character)
    assignment = CharacterAssignment(
        character_id=character.id,
        campaign_id=campaign_id,
        **payload,
    )
    db.add(assignment)
    await db.commit()
    await db.refresh(assignment)

    return CampaignCharacterRead(
        assignment_id=assignment.id,
        character_id=character.id,
        name=character.name,
        character_class=character.character_class,
        level=character.level,
        hp_current=assignment.hp_current,
        hp_max=assignment.hp_max,
    )


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
    if "hp_current" in data:
        assignment.hp_current = data["hp_current"]
    if "hp_max" in data:
        assignment.hp_max = data["hp_max"]
    if "stats" in data and data["stats"] is not None:
        assignment.stats = data["stats"]
    if "inventory" in data and data["inventory"] is not None:
        assignment.inventory = data["inventory"]

    await db.commit()
    await db.refresh(assignment)

    return CampaignCharacterRead(
        assignment_id=assignment.id,
        character_id=character.id,
        name=character.name,
        character_class=character.character_class,
        level=character.level,
        hp_current=assignment.hp_current,
        hp_max=assignment.hp_max,
    )


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

    assignment.ended_at = datetime.now(timezone.utc)
    await db.commit()
    return None
