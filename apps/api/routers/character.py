from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from db.postgres.models import Campaign, Character, CharacterAssignment

from ..dependencies import current_user_id, get_session
from ..schemas.character import (
    CharacterAssignmentHistoryRead,
    CharacterCreate,
    CharacterRead,
    CharacterUpdate,
    CurrentAssignmentSummary,
)
from ..services.character_assignments import resource_view, sheet_view
from ..services.character_presets import kit_error, preset_for_class
from ..services.setup_requests import creation_identity, creation_replay
from ._common import Page

router = APIRouter(tags=["characters"])

# Fields that together must be one supported preset's starting kit (see services.character_presets).
KIT_FIELDS = ("character_class", "base_stats", "base_inventory", "level")


def _kit_rejected(message: str) -> HTTPException:
    return HTTPException(status_code=422, detail={"code": "validation_failed", "message": message})


async def _character_owned_or_404(db: AsyncSession, character_id: int, owner_id: int) -> Character:
    r = await db.execute(
        select(Character).where(Character.id == character_id, Character.owner_user_id == owner_id)
    )
    char = r.scalar_one_or_none()
    if char is None:
        raise HTTPException(status_code=404, detail="Character not found")
    return char


async def _current_assignment_summary(db: AsyncSession, character_id: int) -> CurrentAssignmentSummary | None:
    r = await db.execute(
        select(CharacterAssignment)
        .where(
            CharacterAssignment.character_id == character_id,
            CharacterAssignment.ended_at.is_(None),
        )
        .options(selectinload(CharacterAssignment.campaign))
    )
    ass = r.scalar_one_or_none()
    if ass is None:
        return None
    camp = ass.campaign
    return CurrentAssignmentSummary(campaign_id=camp.id, campaign_title=camp.title)


def _read_with_assignment(char: Character, summary: CurrentAssignmentSummary | None) -> CharacterRead:
    return CharacterRead(
        id=char.id,
        owner_user_id=char.owner_user_id,
        name=char.name,
        character_class=char.character_class,
        level=char.level,
        hp_max=char.hp_max,
        base_stats=char.base_stats,
        base_inventory=char.base_inventory,
        created_at=char.created_at,
        current_assignment=summary,
    )


@router.get("/characters", response_model=list[CharacterRead])
async def list_characters(
    db: AsyncSession = Depends(get_session),
    owner_id: int = Depends(current_user_id),
    page: Page = Depends(),
):
    # One query: each character LEFT JOINed to its (at most one) active assignment's campaign.
    active = (
        select(CharacterAssignment.character_id, Campaign.id, Campaign.title)
        .join(Campaign, Campaign.id == CharacterAssignment.campaign_id)
        .where(CharacterAssignment.ended_at.is_(None))
        .subquery()
    )
    r = await db.execute(
        select(Character, active.c.id, active.c.title)
        .outerjoin(active, active.c.character_id == Character.id)
        .where(Character.owner_user_id == owner_id)
        .order_by(Character.id)
        .limit(page.limit)
        .offset(page.offset)
    )
    return [
        _read_with_assignment(
            ch,
            CurrentAssignmentSummary(campaign_id=camp_id, campaign_title=title)
            if camp_id is not None
            else None,
        )
        for ch, camp_id, title in r.all()
    ]


@router.get("/characters/{character_id}", response_model=CharacterRead)
async def get_character(
    character_id: int,
    db: AsyncSession = Depends(get_session),
    owner_id: int = Depends(current_user_id),
):
    char = await _character_owned_or_404(db, character_id, owner_id)
    summary = await _current_assignment_summary(db, character_id)
    return _read_with_assignment(char, summary)


@router.post("/characters", response_model=CharacterRead, status_code=201)
async def create_character(
    body: CharacterCreate,
    db: AsyncSession = Depends(get_session),
    owner_id: int = Depends(current_user_id),
):
    problem = kit_error(body.character_class, body.base_stats, body.base_inventory, body.level)
    if problem is not None:
        raise _kit_rejected(problem)
    existing = await creation_replay(db, Character, owner_id, body)
    if existing is not None:
        summary = await _current_assignment_summary(db, existing.id)
        return _read_with_assignment(existing, summary)
    character = Character(
        **creation_identity(body),
        owner_user_id=owner_id,
        name=body.name,
        character_class=preset_for_class(body.character_class)["character_class"],
        level=body.level,
        hp_max=body.hp_max,
        base_stats=body.base_stats,
        base_inventory=body.base_inventory,
    )
    db.add(character)
    await db.commit()
    await db.refresh(character)
    return _read_with_assignment(character, None)


@router.patch("/characters/{character_id}", response_model=CharacterRead)
async def update_character(
    character_id: int,
    body: CharacterUpdate,
    db: AsyncSession = Depends(get_session),
    owner_id: int = Depends(current_user_id),
):
    character = await _character_owned_or_404(db, character_id, owner_id)
    update_data = body.model_dump(exclude_unset=True)
    # Only a change to the kit is checked, so a legacy row can still be renamed or re-saved unchanged.
    if any(key in update_data and update_data[key] != getattr(character, key) for key in KIT_FIELDS):
        merged = {key: update_data.get(key, getattr(character, key)) for key in KIT_FIELDS}
        problem = kit_error(merged["character_class"], merged["base_stats"], merged["base_inventory"], merged["level"])
        if problem is not None:
            raise _kit_rejected(problem)
        update_data["character_class"] = preset_for_class(merged["character_class"])["character_class"]
    for key, value in update_data.items():
        setattr(character, key, value)
    await db.commit()
    await db.refresh(character)
    summary = await _current_assignment_summary(db, character_id)
    return _read_with_assignment(character, summary)


@router.delete("/characters/{character_id}", status_code=204)
async def delete_character(
    character_id: int,
    db: AsyncSession = Depends(get_session),
    owner_id: int = Depends(current_user_id),
):
    # The database clears sessions.active_character_id (ON DELETE SET NULL) and removes
    # the character's assignments (ON DELETE CASCADE).
    character = await _character_owned_or_404(db, character_id, owner_id)
    await db.delete(character)
    await db.commit()


@router.get("/characters/{character_id}/assignments", response_model=list[CharacterAssignmentHistoryRead])
async def list_character_assignments(
    character_id: int,
    db: AsyncSession = Depends(get_session),
    owner_id: int = Depends(current_user_id),
    page: Page = Depends(),
):
    character = await _character_owned_or_404(db, character_id, owner_id)
    r = await db.execute(
        select(CharacterAssignment)
        .where(CharacterAssignment.character_id == character_id)
        .options(selectinload(CharacterAssignment.campaign))
        .order_by(desc(CharacterAssignment.assigned_at), desc(CharacterAssignment.id))
        .limit(page.limit)
        .offset(page.offset)
    )
    rows = r.scalars().all()
    out: list[CharacterAssignmentHistoryRead] = []
    for a in rows:
        camp = a.campaign
        ended = a.ended_at is not None
        out.append(
            CharacterAssignmentHistoryRead(
                id=a.id,
                campaign_id=a.campaign_id,
                campaign_title=camp.title,
                hp_current=a.hp_current,
                hp_max=a.hp_max,
                stats=a.stats,
                inventory=a.inventory,
                assigned_at=a.assigned_at,
                ended_at=a.ended_at,
                status="ended" if ended else "active",
                **resource_view(
                    {"class": character.character_class, "level": a.level, "stats": a.stats}
                ),
                **sheet_view(
                    {
                        "class": character.character_class,
                        "level": a.level,
                        "stats": a.stats,
                        "inventory": a.inventory,
                    }
                ),
            )
        )
    return out
