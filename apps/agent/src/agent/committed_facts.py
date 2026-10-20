"""Project exact session and engine actor outcomes into canonical prepared facts."""

from __future__ import annotations

from edgar_core.prepared import CommittedPreparedFact
from edgar_core.story import StoryState

_STORY_KIND = {
    "discover": "entity",
    "encounter": "entity",
    "relationship": "entity",
    "objective": "objective",
    "milestone": "milestone",
    "clock": "clock",
    "acquire": "object",
    "consume": "object",
    "transfer": "object",
    "use": "object",
    "environment": "object",
    "move": "scene",
    "visit": "scene",
    "learn_place": "scene",
}
_TERMINAL_STATUSES = {"completed", "failed", "fulfilled", "broken", "dead", "released"}
_COMBAT_DISPOSITION_STATUS = {
    "captured": "captive",
    "dead": "dead",
    "defeated": "defeated",
}
_COMBAT_STORY_STATUSES = {"captive", "dead", "defeated", "released"}


def committed_prepared_facts(
    state: dict, collection: str, manifest
) -> list[CommittedPreparedFact]:
    """Project current states and permanent revealed evidence for one reviewed version.

    CombatState is session-owned and stores a roster's private source binding. Its numeric row
    ID and row-level victory/defeat label are not adventure entity identities. Only a combatant
    bound to this exact reviewed manifest can contribute an entity fact.
    """
    facts: list[CommittedPreparedFact] = []
    for key, value in sorted((state.get("world_flags") or {}).items()):
        facts.append(
            CommittedPreparedFact(
                kind="world_flag", target_id=key, status=value, value=value, irreversible=True
            )
        )
    passage_by_id = {row.passage_id: row for row in manifest.passages}
    story = StoryState.model_validate(state.get("story_state") or {}).facts
    latest = {
        (item.kind, item.target.canonical_id): item
        for item in story
        if item.target.collection == collection
        and item.target.source_sha256 == manifest.source_sha256
        and item.target.manifest_sha256 == manifest.reviewed_sha256
    }
    for item in story:
        if (
            item.target.collection != collection
            or item.target.source_sha256 != manifest.source_sha256
            or item.target.manifest_sha256 != manifest.reviewed_sha256
        ):
            continue
        target_kind = _STORY_KIND.get(item.kind)
        if target_kind and latest[(item.kind, item.target.canonical_id)] is item:
            passage_ids = sorted(
                {
                    ref["passage_id"]
                    for ref in item.source_refs
                    if isinstance(ref.get("passage_id"), str)
                    and ref.get("audience") == "player"
                    and ref.get("collection") == collection
                    and ref.get("source_sha256") == manifest.source_sha256
                    and ref.get("manifest_sha256") == item.target.manifest_sha256
                }
            )
            facts.append(
                CommittedPreparedFact(
                    kind=target_kind,
                    target_id=item.target.canonical_id,
                    status=item.status,
                    value=(str(item.quantity) if item.quantity is not None else None),
                    passage_ids=passage_ids[:32],
                    irreversible=item.status in _TERMINAL_STATUSES
                    or item.kind in {"consume", "transfer"},
                )
            )
        for ref in item.source_refs:
            passage_id = ref.get("passage_id")
            if (
                not isinstance(passage_id, str)
                or ref.get("audience") != "player"
                or ref.get("collection") != collection
                or ref.get("source_sha256") != manifest.source_sha256
                or ref.get("manifest_sha256") != item.target.manifest_sha256
            ):
                continue
            passage = passage_by_id.get(passage_id)
            if passage is not None and passage.audience == "player":
                facts.append(
                    CommittedPreparedFact(
                        kind="passage",
                        target_id=passage_id,
                        status="revealed",
                        text_sha256=passage.text_sha256,
                        irreversible=True,
                    )
                )
    # The combat row remains an immutable snapshot. A session StoryFact can record the later
    # release or explicit kill of a captive, keyed by its engine-owned combat id and combatant
    # name. Apply that latest status to the exact actor binding in the persisted roster.
    story_combat_status: dict[tuple[int, str], str] = {}
    for item in story:
        if item.kind != "encounter" or item.target.collection != "combat":
            continue
        for ref in item.source_refs:
            combat_id = ref.get("combat_id")
            combatant_id = ref.get("combatant_id")
            if (
                type(combat_id) is int
                and isinstance(combatant_id, str)
                and item.status in _COMBAT_STORY_STATUSES
            ):
                story_combat_status[(combat_id, combatant_id)] = item.status

    actors = {
        entity.entity_id for entity in manifest.entities if entity.kind == "actor"
    }
    latest_actor_facts: dict[str, tuple[int, str]] = {}
    for row in state.get("resolved_combat_outcomes") or []:
        combat_id = row.get("id")
        if type(combat_id) is not int:
            continue
        for combatant in row.get("combatants") or []:
            if combatant.get("is_player"):
                continue
            source = combatant.get("source_reference") or {}
            actor_id = source.get("actor_id")
            if (
                not isinstance(actor_id, str)
                or source.get("actor_collection") != collection
                or source.get("actor_manifest_sha256") != manifest.reviewed_sha256
                or source.get("actor_source_sha256") != manifest.source_sha256
                or actor_id not in actors
            ):
                continue
            status = _COMBAT_DISPOSITION_STATUS.get(combatant.get("disposition"))
            if status is None:
                continue
            combatant_id = combatant.get("name")
            if isinstance(combatant_id, str):
                status = story_combat_status.get((combat_id, combatant_id), status)
            previous = latest_actor_facts.get(actor_id)
            if previous is None or combat_id >= previous[0]:
                latest_actor_facts[actor_id] = (combat_id, status)
    for actor_id, (_, status) in latest_actor_facts.items():
        facts.append(
            CommittedPreparedFact(
                kind="entity", target_id=actor_id, status=status, irreversible=True
            )
        )
    return facts

