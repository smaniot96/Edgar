"""Freeze real selected heroes in a short read transaction before dispatching DM work."""

import copy

from fastapi import HTTPException
from sqlalchemy import select

from db.postgres.models import Character, User

from ..schemas.preparation_setup import PreparationSetup


async def capture_setup(setup: PreparationSetup | None) -> dict | None:
    if setup is None:
        # Retaining the upload is safe before selection. No provider may run until bound.
        return None
    if len(set(setup.character_ids)) != len(setup.character_ids):
        raise HTTPException(status_code=422, detail="Choose each hero once.")
    from db.postgres.session import async_session_factory

    async with async_session_factory() as db:
        owner = (await db.execute(select(User).order_by(User.id).limit(1))).scalar_one_or_none()
        if owner is None:
            raise HTTPException(
                status_code=400, detail="Complete first-run setup before preparation."
            )
        characters = (
            (
                await db.execute(
                    select(Character).where(
                        Character.id.in_(setup.character_ids), Character.owner_user_id == owner.id
                    )
                )
            )
            .scalars()
            .all()
        )
        by_id = {character.id: character for character in characters}
        if set(by_id) != set(setup.character_ids):
            raise HTTPException(status_code=422, detail="A selected hero is no longer available.")
        heroes = [
            {
                "character_id": character.id,
                "name": character.name,
                "character_class": character.character_class,
                "level": character.level,
                "hp_max": character.hp_max,
                "stats": copy.deepcopy(character.base_stats or {}),
                "inventory": copy.deepcopy(character.base_inventory or {}),
            }
            for character in (by_id[identifier] for identifier in setup.character_ids)
        ]
    # No live ORM object, connection, or transaction enters a provider task.
    return {"request": setup.model_dump(), "heroes": heroes}
