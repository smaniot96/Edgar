"""Milestone grants and the level-up transaction (EDG-045).

Both run inside the caller's transaction and never commit:

  * `victory_just_ended` and `grant_victory_milestone` are called by the turn writer. A milestone is
    granted once, on the transition of the active encounter to a victory, under the turn's row lock
    and receipt guard, so a retried turn cannot grant it twice.
  * `apply_level_up` is the explicit player choice. It locks the assignment, checks the campaign
    mode, the level the client saw and any unspent milestone, applies the planned changes and
    records the result on the sheet. A request with the same `request_id` as the last applied one
    returns that stored result unchanged, so a retried request is idempotent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent.progression import (
    MAX_ADVANCED_LEVEL,
    PROGRESSION_MILESTONE,
    apply_asi_to_stats,
    milestone_granted,
    plan_level_up,
)
from agent.resolution import ADVANCEMENT_KEY, character_class
from agent.spells import slot_totals
from db.postgres.models import Campaign, CharacterAssignment, CombatState, Session as SessionModel

from .character_assignments import character_play_state, load_active_assignment


class LevelUpConflict(Exception):
    """A level-up the state does not allow (mode, level, milestone, combat). Maps to HTTP 409."""


@dataclass(frozen=True)
class LevelUpResult:
    applied: bool  # False for an idempotent replay of an earlier request
    record: dict[str, Any]  # the stored advancement record (same shape for a replay)


async def victory_just_ended(db: AsyncSession, session_id: int, combat_state: dict | None) -> bool:
    """True when this turn's encounter moves from open to a victory (not for a re-persisted end)."""
    if not combat_state or not combat_state.get("id"):
        return False
    if not combat_state.get("ended") or combat_state.get("outcome") != "victory":
        return False
    row = (
        await db.execute(
            select(CombatState.ended).where(
                CombatState.id == combat_state["id"], CombatState.session_id == session_id
            )
        )
    ).scalar_one_or_none()
    return row is False


async def grant_victory_milestone(
    db: AsyncSession, campaign: Campaign, character_id: int | None
) -> bool:
    """Grant one milestone to the hero's assignment when the campaign uses milestones.

    Returns whether a milestone was granted. No grant when the hero would go past the level the
    milestones can reach (level + pending must stay below the advancement cap).
    """
    if campaign.progression_mode != PROGRESSION_MILESTONE or not character_id:
        return False
    assignment = await load_active_assignment(db, character_id, campaign.id)
    if assignment is None or not milestone_granted(assignment.level, assignment.milestones_pending):
        return False
    assignment.milestones_pending += 1
    return True


async def campaign_has_open_combat(db: AsyncSession, campaign_id: int) -> bool:
    row = await db.execute(
        select(CombatState.id)
        .join(SessionModel, SessionModel.id == CombatState.session_id)
        .where(SessionModel.campaign_id == campaign_id, CombatState.ended.is_(False))
        .limit(1)
    )
    return row.scalar_one_or_none() is not None


async def apply_level_up(
    db: AsyncSession,
    campaign: Campaign,
    character_id: int,
    *,
    from_level: int,
    request_id: str,
    hp_method: str,
    ability_increases: dict[str, int] | None,
) -> LevelUpResult:
    """Spend one milestone on one level, atomically. Raises LevelUpConflict or ProgressionError."""
    if campaign.progression_mode != PROGRESSION_MILESTONE:
        raise LevelUpConflict(
            "This campaign's progression is fixed. Switch it to milestones first."
        )
    if campaign.status == "ended":
        raise LevelUpConflict("Reopen the campaign before advancing a character.")
    assignment = await load_active_assignment(db, character_id, campaign.id)
    if assignment is None:
        raise LevelUpConflict("No active assignment for this character in this campaign.")
    # Lock the assignment row so two requests cannot both spend the same milestone.
    locked = (
        await db.execute(
            select(CharacterAssignment)
            .where(CharacterAssignment.id == assignment.id)
            .with_for_update()
        )
    ).scalar_one()

    stored = (locked.stats or {}).get(ADVANCEMENT_KEY)
    if isinstance(stored, dict) and stored.get("request_id") == request_id:
        return LevelUpResult(applied=False, record=stored.get("result") or {})

    if await campaign_has_open_combat(db, campaign.id):
        raise LevelUpConflict("Level-up waits until the encounter is over.")
    if locked.level != from_level:
        raise LevelUpConflict("The sheet's level has changed. Reload the sheet and try again.")

    character = await character_play_state(db, character_id, campaign.id)
    if character is None or not character_class(character):
        raise LevelUpConflict("No character sheet for this campaign.")
    if locked.level >= MAX_ADVANCED_LEVEL:
        raise LevelUpConflict(f"Milestone advancement stops at level {MAX_ADVANCED_LEVEL}.")
    plan = plan_level_up(
        character,
        from_level=from_level,
        milestones_pending=locked.milestones_pending,
        hp_method=hp_method,
        asi=ability_increases,
    )
    hp_before = locked.hp_max
    gain = plan.hit_points.amount
    locked.level = plan.to_level
    locked.milestones_pending -= 1
    locked.hp_max = hp_before + gain
    locked.hp_current = min(locked.hp_max, locked.hp_current + gain)
    stats = apply_asi_to_stats(locked.stats, plan.asi)
    # Engine-owned ledgers, kept as they are; the record is the evidence of this level-up.
    record = {
        "request_id": request_id,
        "from_level": plan.from_level,
        "to_level": plan.to_level,
        "hit_points": {
            "amount": gain,
            "method": plan.hit_points.method,
            "dice": plan.hit_points.dice.to_log("level-up hit points")
            if plan.hit_points.dice is not None
            else None,
            "hp_max_before": hp_before,
            "hp_max_after": locked.hp_max,
        },
        "ability_increases": plan.asi,
        "features": plan.features,
        "spell_slots": {
            str(level): total
            for level, total in slot_totals({**character, "level": plan.to_level}).items()
        },
        "milestones_pending": locked.milestones_pending,
    }
    stats[ADVANCEMENT_KEY] = {"request_id": request_id, "result": record}
    locked.stats = stats
    await db.flush()
    return LevelUpResult(applied=True, record=record)
