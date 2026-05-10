"""Apply structured adjudication outcomes to PostgreSQL.

Runs inside the API's request transaction (the caller is responsible for `commit()`), so HP
deltas, scene transitions, world flags, and inventory changes land atomically with the
narration row written by the turn endpoint. This is the source-of-truth writer; the agent's
`world_state_updater_node` only writes a telemetry row to event_log.

Invariants:
  - HP is clamped to [0, hp_max].
  - Conditions and inventory are stored inside the JSONB columns; we treat them as sets/lists
    and replace the whole field rather than mutate in place, so SQLAlchemy notices the change.
  - Flag writes are upserts; clears are bulk DELETEs scoped to the campaign.
  - Per-campaign character state lives on `CharacterAssignment` (plan 03), not `Character`.
"""

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from agent.models.adjudication import AdjudicationResult
from db.postgres.models import Session as SessionModel, WorldFlag

from .character_assignments import load_active_assignment


class MissingCharacterAssignmentError(RuntimeError):
    """No active assignment for (active_character_id, session.campaign_id)."""


async def apply_adjudication(
    db: AsyncSession,
    game_session: SessionModel,
    adj: AdjudicationResult,
) -> None:
    if adj.scene_id and adj.scene_id != game_session.current_scene_id:
        game_session.current_scene_id = adj.scene_id

    if adj.character_update is not None and game_session.active_character_id:
        await _apply_character_update(
            db,
            game_session.active_character_id,
            game_session.campaign_id,
            adj.character_update,
        )

    for fu in adj.flags_set:
        existing = await db.execute(
            select(WorldFlag).where(
                WorldFlag.campaign_id == game_session.campaign_id,
                WorldFlag.key == fu.key,
            )
        )
        flag = existing.scalar_one_or_none()
        if flag:
            flag.value = fu.value
        else:
            db.add(WorldFlag(campaign_id=game_session.campaign_id, key=fu.key, value=fu.value))

    if adj.flags_cleared:
        await db.execute(
            delete(WorldFlag).where(
                WorldFlag.campaign_id == game_session.campaign_id,
                WorldFlag.key.in_(adj.flags_cleared),
            )
        )


async def _apply_character_update(db, character_id: int, campaign_id: int, cu) -> None:
    ass = await load_active_assignment(db, character_id, campaign_id)
    if ass is None:
        raise MissingCharacterAssignmentError(
            f"No active assignment for character_id={character_id} campaign_id={campaign_id}"
        )

    if cu.hp_delta is not None:
        ass.hp_current = max(0, min(ass.hp_max, ass.hp_current + cu.hp_delta))

    stats = dict(ass.stats or {})
    conditions = list(stats.get("conditions", []))
    for name in cu.add_conditions:
        if name not in conditions:
            conditions.append(name)
    for name in cu.remove_conditions:
        if name in conditions:
            conditions.remove(name)
    stats["conditions"] = conditions
    ass.stats = stats

    inv = dict(ass.inventory or {})
    items = list(inv.get("items", []))
    items.extend(cu.inventory_add)
    for it in cu.inventory_remove:
        if it in items:
            items.remove(it)
    inv["items"] = items
    ass.inventory = inv
