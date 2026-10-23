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
    CLASS_FEATURES_KEY,
    COMPLETION_FLAG,
    DEATH_SAVES_KEY,
    HIT_DICE_KEY,
    SECOND_WIND_KEY,
    match_inventory_item,
    normalise_conditions,
    sanitize_flags,
    sanitize_inventory_add,
    validate_scene_id,
)
from db.postgres.models import Session as SessionModel, WorldFlag

from .character_assignments import load_active_assignment
from .world_flags import upsert_world_flag

log = structlog.get_logger()


class MissingCharacterAssignmentError(RuntimeError):
    """No active assignment for (active_character_id, session.campaign_id)."""


async def apply_adjudication(
    db: AsyncSession,
    game_session: SessionModel,
    adj: AdjudicationResult,
    *,
    session_discovery_keys: set[str] | None = None,
) -> None:
    if adj.scene_id:
        known = {game_session.current_scene_id} if game_session.current_scene_id else set()
        # Validated moves carry the exact publication identity. A similar previous
        # event ID is not a spelling correction for that canonical destination.
        known.update(
            effect.target.canonical_id
            for effect in adj.accepted_story_effects
            if effect.kind == "move" and effect.target.kind == "scene"
        )
        scene_id = validate_scene_id(adj.scene_id, known)
        if scene_id is None:
            log.warning("scene_id_rejected", scene_id=str(adj.scene_id)[:80])
        elif scene_id != game_session.current_scene_id:
            game_session.current_scene_id = scene_id

    if adj.character_update is not None and game_session.active_character_id:
        cu = adj.character_update.model_copy(deep=True)
        # The typed object projection already applies these exact costs/acquisitions once.
        # Retain engine HP/resources while preventing a legacy name removal from consuming
        # another same-name item after the stable object has been removed.
        acquired_names = {
            effect.display_name.casefold()
            for effect in adj.accepted_story_effects
            if effect.kind == "acquire" and effect.display_name
        }
        removed_names = {
            effect.display_name.casefold()
            for effect in adj.accepted_story_effects
            if effect.kind in {"consume", "transfer"} and effect.display_name
        }
        cu.inventory_add = [
            name for name in cu.inventory_add if name.casefold() not in acquired_names
        ]
        cu.inventory_remove = [
            name for name in cu.inventory_remove if name.casefold() not in removed_names
        ]
        await _apply_character_update(
            db,
            game_session.active_character_id,
            game_session.campaign_id,
            cu,
        )

    completion_ok = adj.completion_status in (None, "accepted")
    for fu in sanitize_flags(adj.flags_set, allow_reserved=True):
        if fu.key in (session_discovery_keys or set()):
            continue
        if fu.key == COMPLETION_FLAG and not completion_ok:
            log.warning("completion_flag_rejected", status=adj.completion_status)
            continue
        await upsert_world_flag(db, game_session.campaign_id, fu.key, fu.value)

    if adj.flags_cleared:
        wanted = [
            str(k) for k in adj.flags_cleared if str(k) not in (session_discovery_keys or set())
        ]
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
    if cu.second_wind_uses_remaining is not None:
        # Engine-owned sheet resource (EDG-025); persists across encounters and rests.
        stats[SECOND_WIND_KEY] = {"uses_remaining": max(0, int(cu.second_wind_uses_remaining))}
    if cu.class_feature_uses:
        # Engine-owned class feature uses (EDG-145): only the written features change.
        features = dict(stats.get(CLASS_FEATURES_KEY) or {})
        for feature, left in cu.class_feature_uses.items():
            features[feature] = {"uses_remaining": max(0, int(left))}
        stats[CLASS_FEATURES_KEY] = features
    if cu.hit_dice_spent is not None:
        # Engine-owned Hit Dice ledger (EDG-026): a short rest spends one, a long rest recovers some.
        stats[HIT_DICE_KEY] = {"spent": max(0, int(cu.hit_dice_spent))}
    death_saves = getattr(cu, "death_saves", None)  # absent on duck-typed updates: no change
    if death_saves is not None:
        # Engine-owned death record (EDG-045): written as a dict, or {} to clear it.
        if death_saves:
            stats[DEATH_SAVES_KEY] = dict(death_saves)
        else:
            stats.pop(DEATH_SAVES_KEY, None)
    if ass.hp_current > 0:
        # Any healing that leaves the hero above 0 HP ends unconsciousness and its death record.
        stats.pop(DEATH_SAVES_KEY, None)
    conditions = list(stats.get("conditions", []))
    for name in normalise_conditions(cu.add_conditions):
        if name not in conditions:
            conditions.append(name)
    for name in normalise_conditions(cu.remove_conditions):
        if name in conditions:
            conditions.remove(name)
    stats["conditions"] = conditions
    if cu.spell_slots_expended is not None:
        # Engine-owned expended slot ledger (agent.spells); totals derive from class and level.
        stats["spell_slots_expended"] = {
            str(level): int(count)
            for level, count in cu.spell_slots_expended.items()
            if int(count) > 0
        }
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


async def apply_story_inventory(db, game_session, effects, *, initial_state: dict) -> None:
    """Project accepted object ownership into the existing assignment inside the turn tx.

    Stable identity stays in the ledger after movement/consumption. Names are display text;
    removal uses the canonical identity, with reviewed exact-name binding for legacy gear.
    """
    from agent.object_effects import OBJECT_KINDS, project_object_inventory, validate_object_effect
    from edgar_core.story import StoryEffect

    objects = [effect for effect in effects if effect.kind in OBJECT_KINDS]
    if not objects:
        return
    ass = await load_active_assignment(
        db, game_session.active_character_id, game_session.campaign_id
    )
    if ass is None:
        raise MissingCharacterAssignmentError("No active character assignment for object effects")
    validated = []
    for accepted in objects:
        effect = StoryEffect.model_validate(
            accepted.model_dump(include=set(StoryEffect.model_fields))
        )
        normalized = validate_object_effect(effect, initial_state, validated)
        if normalized is None or normalized != effect:
            raise ValueError("Story object availability changed before commit")
        validated.append(accepted)
    ass.inventory = project_object_inventory(ass.inventory, validated, initial_state)
