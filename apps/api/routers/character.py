from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.postgres.models import Character
from ..dependencies import get_session
from ..schemas.character import CharacterCreate, CharacterRead, CharacterUpdate

router = APIRouter(tags=["characters"])


@router.get("/characters", response_model=list[CharacterRead])
async def list_characters(db: AsyncSession = Depends(get_session)):
    result = await db.execute(select(Character))
    characters = result.scalars().all()
    return [CharacterRead.model_validate(c) for c in characters]


@router.get("/characters/{character_id}", response_model=CharacterRead)
async def get_character(character_id: int, db: AsyncSession = Depends(get_session)):
    result = await db.execute(select(Character).where(Character.id == character_id))
    character = result.scalar_one_or_none()
    if character is None:
        raise HTTPException(status_code=404, detail="Character not found")
    return CharacterRead.model_validate(character)


@router.post("/characters", response_model=CharacterRead)
async def create_character(body: CharacterCreate, db: AsyncSession = Depends(get_session)):
    character = Character(
        campaign_id=body.campaign_id,
        name=body.name,
        character_class=body.character_class,
        level=body.level,
        hp_current=body.hp_current,
        hp_max=body.hp_max,
        stats=body.stats,
        inventory=body.inventory,
    )
    db.add(character)
    await db.commit()
    await db.refresh(character)
    return CharacterRead.model_validate(character)


@router.patch("/characters/{character_id}", response_model=CharacterRead)
async def update_character(
    character_id: int, body: CharacterUpdate, db: AsyncSession = Depends(get_session)
):
    result = await db.execute(select(Character).where(Character.id == character_id))
    character = result.scalar_one_or_none()
    if character is None:
        raise HTTPException(status_code=404, detail="Character not found")
    update_data = body.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(character, key, value)
    await db.commit()
    await db.refresh(character)
    return CharacterRead.model_validate(character)


@router.delete("/characters/{character_id}", status_code=204)
async def delete_character(character_id: int, db: AsyncSession = Depends(get_session)):
    result = await db.execute(select(Character).where(Character.id == character_id))
    character = result.scalar_one_or_none()
    if character is None:
        raise HTTPException(status_code=404, detail="Character not found")
    await db.delete(character)
    await db.commit()
