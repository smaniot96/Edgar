"""Character ↔ campaign assignment helpers (plan 03)."""

from __future__ import annotations

import copy
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from db.postgres.models import Character, CharacterAssignment


async def load_active_assignment(
    db: AsyncSession, character_id: int, campaign_id: int
) -> CharacterAssignment | None:
    r = await db.execute(
        select(CharacterAssignment)
        .where(
            CharacterAssignment.character_id == character_id,
            CharacterAssignment.campaign_id == campaign_id,
            CharacterAssignment.ended_at.is_(None),
        )
        .options(selectinload(CharacterAssignment.character))
    )
    return r.scalar_one_or_none()


async def load_active_assignment_any_campaign(
    db: AsyncSession, character_id: int
) -> CharacterAssignment | None:
    r = await db.execute(
        select(CharacterAssignment)
        .where(
            CharacterAssignment.character_id == character_id,
            CharacterAssignment.ended_at.is_(None),
        )
        .options(selectinload(CharacterAssignment.character))
    )
    return r.scalar_one_or_none()


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
    return {
        "id": char.id,
        "name": char.name,
        "class": char.character_class,
        "level": char.level,
        "hp_current": assignment.hp_current,
        "hp_max": assignment.hp_max,
        "stats": assignment.stats,
        "inventory": assignment.inventory,
    }
