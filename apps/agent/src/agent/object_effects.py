"""Validate qualitative consequences for reviewed, stable objects.

The session story ledger is authoritative. Assignment inventory is its compatible carried-item
projection; no model quantity, bonus, damage or resource value is applied here.
"""

from __future__ import annotations

from edgar_core.manifests import require_published
from edgar_core.story import StoryEffect, StoryRef, story_target_id

OBJECT_KINDS = {"acquire", "consume", "transfer", "use", "environment"}


def _dict(value):
    return value.model_dump() if hasattr(value, "model_dump") else value


def object_definition(effect: StoryEffect, state: dict):
    raw = (state.get("adventure_manifests") or {}).get(effect.target.collection)
    if raw is None or effect.target.kind != "object":
        return None
    manifest = require_published(raw)
    if (
        manifest.source_sha256 != effect.target.source_sha256
        or manifest.reviewed_sha256 != effect.target.manifest_sha256
    ):
        return None
    return next(
        (obj for obj in manifest.objects if obj.canonical_id == effect.target.canonical_id), None
    )


def inventory_entries(character: dict | None):
    for entries in ((character or {}).get("inventory") or {}).values():
        if isinstance(entries, list):
            yield from entries


def object_state(target: StoryRef, state: dict, accepted_so_far=()) -> dict:
    """Replay the same committed facts used by reload/planning, retaining consumed identity."""
    identity = story_target_id(target)
    current = {"owner": None, "consumed": False, "known": False, "used": False}
    facts = list((state.get("story_state") or {}).get("facts", [])) + list(accepted_so_far)
    for value in facts:
        fact = _dict(value)
        if story_target_id(StoryRef.model_validate(fact["target"])) != identity:
            continue
        kind = fact["kind"]
        current["known"] = True
        if kind == "acquire":
            current["owner"] = fact.get("destination")
        elif kind == "consume":
            current["consumed"] = True
            current["owner"] = None
        elif kind == "transfer":
            current["owner"] = fact.get("destination")
        elif kind == "use":
            current["used"] = True
    return current


def validate_object_effect(
    effect: StoryEffect, state: dict, accepted_so_far=()
) -> StoryEffect | None:
    definition = object_definition(effect, state)
    if definition is None or effect.quantity not in (None, 1):
        return None
    character = state.get("character") or {}
    owner = f"character:{character.get('id')}" if character.get("id") else None
    current = object_state(effect.target, state, accepted_so_far)
    identity = story_target_id(effect.target)
    entries = list(inventory_entries(character))
    carried = any(isinstance(it, dict) and it.get("story_object_id") == identity for it in entries)
    # Legacy gear can be bound only by an explicit reviewed initial-owner declaration.
    legacy = definition.initial_owner == "character" and any(
        (it.get("name", "") if isinstance(it, dict) else str(it)).casefold()
        == definition.title.casefold()
        for it in entries
    )
    owned = bool(
        owner
        and not current["consumed"]
        and (current["owner"] == owner or (not current["owner"] and (carried or legacy)))
    )
    if current["consumed"]:
        return None
    if effect.kind == "acquire":
        if not owner or not definition.portable or carried or owned:
            return None
        location = current["owner"] or definition.initial_owner
        if location and location != state.get("current_scene_id"):
            return None
        if effect.destination not in (None, owner) or effect.operation is not None:
            return None
        return effect.model_copy(update={"status": "acquired", "destination": owner, "quantity": 1})
    if effect.kind == "consume":
        if (
            not owned
            or not definition.consumable
            or effect.destination is not None
            or effect.operation is not None
        ):
            return None
        return effect.model_copy(update={"status": "consumed", "quantity": 1})
    if effect.kind == "transfer":
        if not owned or not definition.portable or effect.operation is not None:
            return None
        if effect.destination != state.get("current_scene_id"):
            return None
        return effect.model_copy(update={"status": "moved", "quantity": 1})
    if effect.kind == "use":
        if not owned or effect.operation not in definition.operations:
            return None
        if effect.destination:
            raw = (state.get("adventure_manifests") or {})[effect.target.collection]
            manifest = require_published(raw)
            destination = next(
                (obj for obj in manifest.objects if obj.canonical_id == effect.destination), None
            )
            if (
                destination is None
                or state.get("current_scene_id") not in destination.scene_ids
                or not all(
                    (state.get("world_flags") or {}).get(key) == value
                    for key, value in destination.requires.items()
                )
            ):
                return None
        return effect.model_copy(update={"status": "used", "quantity": 1})
    if effect.kind == "environment":
        if effect.operation not in definition.operations or effect.destination is not None:
            return None
        # If gear is cited as the justification it must be an earned, validated use aimed at
        # this exact environment identity. Definition/source checks are also done upstream.
        if effect.knowledge_fact_ids:
            facts = list((state.get("story_state") or {}).get("facts", [])) + list(accepted_so_far)
            uses = [_dict(f) for f in facts if _dict(f).get("kind") == "use"]
            if not any(
                (
                    f.get("fact_id") in effect.knowledge_fact_ids
                    or f.get("effect_id") in effect.knowledge_fact_ids
                )
                and f.get("destination") == effect.target.canonical_id
                for f in uses
            ):
                return None
        return effect.model_copy(update={"status": "changed", "quantity": None})
    return None
