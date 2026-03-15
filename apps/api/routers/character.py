from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from db.postgres.models import Character
from dependencies import get_session
from schemas.character import CharacterCreate, CharacterRead

router = APIRouter(tags=["characters"])


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
