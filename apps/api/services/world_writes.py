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

Validation (defence in depth; the agent engine already enforced the same rules with the
turn's dice — see `agent.resolution.enforce_side_effects`, which is where HP deltas are
justified: damage only after a failed check, engine-rolled; healing only via an inventory
consumable or a rest):
  - `scene_id` must be a snake_case id (<= 64 chars); junk is logged and dropped.
  - flag keys are normalised to snake_case and capped per turn; `flags_cleared` may only name
    keys that exist for the campaign; `campaign_complete` is only written when the engine
    accepted completion.
  - conditions must be official 5e conditions; inventory additions are capped and sanitised;
    removals only remove items actually carried (any inventory list, case-insensitive).
"""

import structlog
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from agent.models.adjudication import AdjudicationResult
from agent.resolution import (
    COMPLETION_FLAG,
    match_inventory_item,
    normalise_conditions,
    sanitize_flags,
    sanitize_inventory_add,
    validate_scene_id,
)
from db.postgres.models import Session as SessionModel, WorldFlag

from .character_assignments import load_active_assignment

log = structlog.get_logger()


class MissingCharacterAssignmentError(RuntimeError):
    """No active assignment for (active_character_id, session.campaign_id)."""


async def apply_adjudication(
    db: AsyncSession,
    game_session: SessionModel,
    adj: AdjudicationResult,
) -> None:
    if adj.scene_id:
        known = {game_session.current_scene_id} if game_session.current_scene_id else set()
        scene_id = validate_scene_id(adj.scene_id, known)
        if scene_id is None:
            log.warning("scene_id_rejected", scene_id=str(adj.scene_id)[:80])
        elif scene_id != game_session.current_scene_id:
            game_session.current_scene_id = scene_id

    if adj.character_update is not None and game_session.active_character_id:
        await _apply_character_update(
            db,
            game_session.active_character_id,
            game_session.campaign_id,
            adj.character_update,
        )

    completion_ok = adj.completion_status in (None, "accepted")
    for fu in sanitize_flags(adj.flags_set, allow_reserved=True):
        if fu.key == COMPLETION_FLAG and not completion_ok:
            log.warning("completion_flag_rejected", status=adj.completion_status)
            continue
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
        wanted = [str(k) for k in adj.flags_cleared]
        rows = await db.execute(
            select(WorldFlag.key).where(
                WorldFlag.campaign_id == game_session.campaign_id,
                WorldFlag.key.in_(wanted),
            )
        )
        existing_keys = set(rows.scalars().all())
        unknown = [k for k in wanted if k not in existing_keys]
        if unknown:
            log.info("flags_cleared_unknown_keys_dropped", keys=unknown[:10])
        if existing_keys:
            await db.execute(
                delete(WorldFlag).where(
                    WorldFlag.campaign_id == game_session.campaign_id,
                    WorldFlag.key.in_(existing_keys),
                )
            )


async def _apply_character_update(db, character_id: int, campaign_id: int, cu) -> None:
    ass = await load_active_assignment(db, character_id, campaign_id)
    if ass is None:
        raise MissingCharacterAssignmentError(
            f"No active assignment for character_id={character_id} campaign_id={campaign_id}"
        )

    if cu.hp_delta is not None:
        # Engine-justified delta (see module docstring); clamp to [0, hp_max] as the invariant.
        ass.hp_current = max(0, min(ass.hp_max, ass.hp_current + cu.hp_delta))

    stats = dict(ass.stats or {})
    conditions = list(stats.get("conditions", []))
    for name in normalise_conditions(cu.add_conditions):
        if name not in conditions:
            conditions.append(name)
    for name in normalise_conditions(cu.remove_conditions):
        if name in conditions:
            conditions.remove(name)
    stats["conditions"] = conditions
    ass.stats = stats

    inv = {k: (list(v) if isinstance(v, list) else v) for k, v in (ass.inventory or {}).items()}
    items = list(inv.get("items", []))
    for it in sanitize_inventory_add(cu.inventory_add):
        items.append(it)
    inv["items"] = items
    for it in cu.inventory_remove:
        _remove_item(inv, it)
    ass.inventory = inv


def _remove_item(inv: dict, name: str) -> bool:
    """Remove one carried item matching `name` from any inventory list ("items" first)."""
    keys = ["items"] + [k for k, v in inv.items() if k != "items" and isinstance(v, list)]
    for key in keys:
        entries = inv.get(key)
        if not isinstance(entries, list):
            continue
        names = [e.get("name", "") if isinstance(e, dict) else str(e) for e in entries]
        match = match_inventory_item(names, name)
        if match is not None:
            entries.pop(names.index(match))
            return True
    log.info("inventory_remove_missing_item", item=str(name)[:80])
    return False
