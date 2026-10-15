"""Character ↔ campaign assignment helpers (plan 03)."""

from __future__ import annotations

import copy
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from agent.resolution import second_wind_status
from db.postgres.models import Character, CharacterAssignment


async def load_active_assignment(
    db: AsyncSession, character_id: int, campaign_id: int | None
) -> CharacterAssignment | None:
    """Load a character's active assignment, optionally scoped to one campaign."""
    query = (
        select(CharacterAssignment)
        .where(
            CharacterAssignment.character_id == character_id,
            CharacterAssignment.ended_at.is_(None),
        )
        .options(selectinload(CharacterAssignment.character))
    )
    if campaign_id is not None:
        query = query.where(CharacterAssignment.campaign_id == campaign_id)
    r = await db.execute(query)
    return r.scalar_one_or_none()


async def load_active_assignment_any_campaign(
    db: AsyncSession, character_id: int
) -> CharacterAssignment | None:
    return await load_active_assignment(db, character_id, campaign_id=None)


def seed_assignment_row_payload(character: Character) -> dict[str, Any]:
    """Snapshot base_* into assignment mutable columns."""
    return {
        "hp_current": character.hp_max,
        "hp_max": character.hp_max,
        "stats": copy.deepcopy(character.base_stats or {}),
        "inventory": copy.deepcopy(character.base_inventory or {}),
    }


async def character_play_state(
    db: AsyncSession, character_id: int | None, campaign_id: int
) -> dict[str, Any] | None:
    """Merged library identity + active assignment stats for agent initial_state / turn responses."""
    if not character_id:
        return None
    assignment = await load_active_assignment(db, character_id, campaign_id)
    if assignment is None:
        return None
    char = assignment.character
    out = {
        "id": char.id,
        "name": char.name,
        "class": char.character_class,
        "level": char.level,
        "hp_current": assignment.hp_current,
        "hp_max": assignment.hp_max,
        "stats": assignment.stats,
        "inventory": assignment.inventory,
    }
    # Additive, derived view of the Second Wind resource (fighters only).
    second_wind = second_wind_status(out)
    if second_wind is not None:
        out["second_wind"] = second_wind
    return out
