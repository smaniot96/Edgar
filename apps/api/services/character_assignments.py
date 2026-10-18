"""Character ↔ campaign assignment helpers (plan 03)."""

from __future__ import annotations

import copy
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from agent.class_features import class_feature_status
from agent.combat_setup import _player_ac
from agent.conditions import save_modifier
from agent.resolution import (
    ABILITIES,
    check_spec,
    hit_dice_status,
    save_proficiencies,
    second_wind_status,
    skill_proficiencies,
)
from agent.spells import castable_spells, expended_slots, slot_totals
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
    out.update(resource_view(out))
    return out


def resource_view(character: dict[str, Any]) -> dict[str, Any]:
    """Additive, read-only resource views derived from class, level and stored stats.

    Keys are present only where the class has the resource; nothing is stored or rewritten here.
    """
    out: dict[str, Any] = {}
    second_wind = second_wind_status(character)
    if second_wind is not None:
        out["second_wind"] = second_wind
    # Additive, derived view of the Hit Dice ledger (EDG-026): every class has Hit Dice.
    out["hit_dice"] = hit_dice_status(character)
    spell_slots = spell_slots_status(character)
    if spell_slots is not None:
        out["spell_slots"] = spell_slots
    # Additive view of the class feature uses (EDG-145); absent for classes without them.
    class_features = class_feature_status(character)
    if class_features is not None:
        out["class_features"] = class_features
    return out


def sheet_view(character: dict[str, Any]) -> dict[str, Any]:
    """Read-only sheet projection (EDG-173): armour class, castable spells and proficiencies.

    Every number is the engine's own: the AC the combat rolls use (`_player_ac`), the catalog spells
    the class can cast (`castable_spells`), and the saving-throw and skill modifiers the checks use.
    `spells` is present only for a class with a castable spell. Nothing is stored or rewritten here.
    """
    out: dict[str, Any] = {
        "armor_class": _player_ac(character),
        "proficiencies": {
            "saving_throws": [
                {"ability": ability, "modifier": save_modifier(character, ability)}
                for ability in ABILITIES
                if ability in save_proficiencies(character)
            ],
            "skills": [
                {"skill": skill, "modifier": check_spec(character, skill).modifier}
                for skill in sorted(skill_proficiencies(character))
            ],
        },
    }
    spells = castable_spells(character)
    if spells:
        out["spells"] = spells
    return out


def spell_slots_status(character: dict[str, Any] | None) -> dict[str, dict[str, int]] | None:
    """Public view of spell slots per level, e.g. {"1": {"total": 4, "remaining": 3}}.

    None for non-casters. Expended counts above the derived total are clamped to zero remaining.
    """
    totals = slot_totals(character)
    if not totals:
        return None
    spent = expended_slots(character)
    return {
        str(level): {
            "total": total,
            "remaining": total - min(total, spent.get(str(level), 0)),
        }
        for level, total in totals.items()
    }
