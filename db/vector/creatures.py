"""Exact identity for indivisible creature records produced by verified extraction.

This only identifies records; it does not interpret or implement their mechanics. A
generic heading, a creature mentioned in prose, or a mixed stat block is not an identity.
"""

from __future__ import annotations

import json
import re
from typing import Any

from db.vector.references import normalize_reference, reference_matches
from edgar_core.artifacts import text_digest

_AC = re.compile(r"\bArmor Class\W{0,6}\d", re.IGNORECASE)
_HP = re.compile(r"\bHit Points\W{0,6}\d", re.IGNORECASE)
_STAT_START = re.compile(
    r"^(?:tiny|small|medium|large|huge|gargantuan)\b|^armor class\b", re.IGNORECASE
)


class CreatureResolutionError(ValueError):
    """An encounter needs an exact source record or a reviewed module actor reference."""


def creature_payload_fields(chunk: dict[str, Any]) -> dict[str, str]:
    """Stamp only a single complete, explicitly headed stat record.

    EDG-002 owns extraction/readiness and complete boundaries. These extra checks reject
    legacy mixed records and conflicting metadata; they never certify arbitrary prose.
    """
    text = chunk.get("text")
    if chunk.get("stat_block") is not True or not isinstance(text, str):
        return {}
    if len(_AC.findall(text)) != 1 or len(_HP.findall(text)) != 1:
        return {}
    section = chunk.get("section")
    if not isinstance(section, str) or not section.strip():
        return {}
    header = next((line.strip(" #*_`") for line in text.splitlines() if line.strip()), "")
    # The chunker prefixes the full heading path, retaining the leaf creature heading.
    header = header.rsplit(" > ", 1)[-1]
    if not reference_matches(header, section):
        return {}
    # A prefixed section cannot hide a second creature's title before its stat fields.
    # The verified chunker emits only the size/type or AC after the identity heading.
    for line in [line.strip(" #*_`") for line in text.splitlines() if line.strip()][1:]:
        if _STAT_START.match(line):
            break
        if not reference_matches(line, section):
            return {}
    key = normalize_reference(section)
    if not key or any(
        chunk.get(field) is not None
        and (not isinstance(chunk[field], str) or not reference_matches(chunk[field], section))
        for field in ("creature_name", "creature_key")
    ):
        return {}
    return {"creature_name": section.strip(), "creature_key": key}


def identified_creature(chunk: dict[str, Any]) -> dict[str, Any] | None:
    """Return the full record with stable edition/source identity, without truncating text."""
    fields = creature_payload_fields(chunk)
    if not fields:
        return None
    identity = {
        "collection": chunk.get("collection"),
        "source": chunk.get("source"),
        "source_sha256": chunk.get("source_sha256"),
        "extraction_sha256": chunk.get("extraction_sha256"),
        "rules_edition": chunk.get("rules_edition", "unspecified"),
        "creature_key": fields["creature_key"],
        "text_sha256": text_digest(chunk["text"]),
    }
    # Legacy and canonical collection aliases of the same source remain distinct citations.
    return {
        **chunk,
        **fields,
        "creature_id": text_digest(json.dumps(identity, sort_keys=True, separators=(",", ":"))),
        "identity_status": "resolved",
    }


def resolve_creature(
    name: str,
    *,
    client=None,
    rules_edition: str = "2014",
    adventure_manifests: dict[str, dict] | None = None,
    current_scene_id: str | None = None,
    world_flags: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Resolve a creature or a reviewed active-scene actor, with an explicit failure.

    Named actors use only their manifest's binding. They never implicitly inherit a
    semantically similar template. Private module records stay private to the caller.
    """
    from db.vector.retrieval import retrieve_active_scene, retrieve_monster
    from edgar_core.manifests import require_published, select_scene_context

    def unresolved(reason: str):
        raise CreatureResolutionError(
            f"Creature identity unresolved for {name!r}: {reason}. "
            "Use the exact verified creature name or review this actor's statistics reference."
        )

    manifests = {
        collection: require_published(value)
        for collection, value in (adventure_manifests or {}).items()
    }
    actors = [
        (collection, manifest, entity)
        for collection, manifest in manifests.items()
        for entity in manifest.entities
        if entity.kind == "actor" and reference_matches(entity.title, name)
    ]
    if len(actors) > 1:
        unresolved("multiple reviewed actors have this name")
    actor_identity = {}
    lookup = {}
    creature_name = name
    if actors:
        collection, manifest, actor = actors[0]
        selection = select_scene_context(manifest, current_scene_id, world_flags)
        if actor.entity_id not in {e["entity_id"] for e in selection["entities"]}:
            unresolved("the actor is outside the unlocked active scene")
        if manifest.rules_edition != rules_edition or actor.stat_reference is None:
            unresolved("the actor has no matching edition and explicit statistics reference")
        reference = actor.stat_reference
        creature_name = reference.creature_name
        actor_identity = {
            "actor_id": actor.entity_id,
            "actor_name": actor.title,
            "actor_collection": collection,
            "actor_manifest_sha256": manifest.reviewed_sha256,
        }
        if reference.kind == "module":
            selected = selection["player_passage_ids"] + selection["planner_passage_ids"]
            if reference.passage_id not in selected:
                unresolved("the module statistics passage is locked or outside the active scene")
            scene = retrieve_active_scene(
                {collection: manifest.model_dump()},
                current_scene_id,
                world_flags,
                client=client,
                rules_edition=rules_edition,
            )
            chunks = scene["adventure_context"] + scene["planner_context"]
            chunk = next((c for c in chunks if c.get("passage_id") == reference.passage_id), None)
            record = identified_creature(
                {
                    **(chunk or {}),
                    "section": creature_name,
                    "stat_block": True,
                }
            )
            if not record:
                unresolved("the reviewed module passage is not one complete named stat record")
            return {**record, **actor_identity}
        lookup = {
            "collections": [reference.collection],
            "source_sha256": reference.source_sha256,
        }
    records = retrieve_monster(
        creature_name,
        client=client,
        rules_edition=rules_edition,
        limit=2,
        **lookup,
    )
    if len(records) != 1:
        unresolved("no unique exact complete source record is available")
    if not all(
        isinstance(records[0].get(field), str)
        and re.fullmatch(r"[0-9a-f]{64}", records[0][field])
        for field in ("source_sha256", "extraction_sha256")
    ):
        unresolved("the source has no verified extraction provenance; review and reingest it")
    return {**records[0], **actor_identity}
