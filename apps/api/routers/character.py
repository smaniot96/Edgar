from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, desc, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from db.postgres.models import Character, CharacterAssignment, Session as SessionModel
from ..dependencies import current_user_id, get_session
from ..schemas.character import (
    CharacterAssignmentHistoryRead,
    CharacterCreate,
    CharacterRead,
    CharacterUpdate,
    CurrentAssignmentSummary,
)

router = APIRouter(tags=["characters"])


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
):
    r = await db.execute(select(Character).where(Character.owner_user_id == owner_id))
    chars = r.scalars().all()
    out: list[CharacterRead] = []
    for ch in chars:
        summary = await _current_assignment_summary(db, ch.id)
        out.append(_read_with_assignment(ch, summary))
    return out


@router.get("/characters/{character_id}", response_model=CharacterRead)
async def get_character(
    character_id: int,
    db: AsyncSession = Depends(get_session),
    owner_id: int = Depends(current_user_id),
):
    char = await _character_owned_or_404(db, character_id, owner_id)
    summary = await _current_assignment_summary(db, character_id)
    return _read_with_assignment(char, summary)


@router.post("/characters", response_model=CharacterRead)
async def create_character(
    body: CharacterCreate,
    db: AsyncSession = Depends(get_session),
    owner_id: int = Depends(current_user_id),
):
    character = Character(
        owner_user_id=owner_id,
        name=body.name,
        character_class=body.character_class,
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
    character = await _character_owned_or_404(db, character_id, owner_id)
    await db.execute(
        update(SessionModel)
        .where(SessionModel.active_character_id == character_id)
        .values(active_character_id=None)
    )
    await db.execute(delete(CharacterAssignment).where(CharacterAssignment.character_id == character_id))
    await db.delete(character)
    await db.commit()


@router.get("/characters/{character_id}/assignments", response_model=list[CharacterAssignmentHistoryRead])
async def list_character_assignments(
    character_id: int,
    db: AsyncSession = Depends(get_session),
    owner_id: int = Depends(current_user_id),
):
    await _character_owned_or_404(db, character_id, owner_id)
    r = await db.execute(
        select(CharacterAssignment)
        .where(CharacterAssignment.character_id == character_id)
        .options(selectinload(CharacterAssignment.campaign))
        .order_by(desc(CharacterAssignment.assigned_at))
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
            )
        )
    return out
