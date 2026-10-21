"""Recover invalid typed joins without reauthoring accepted chapter content.

The ordinary path makes no semantic call. Defective bundles receive one scoped
repair with their original evidence; its complete candidate is checked atomically.
"""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy

from edgar_core.campaign_semantics import Bundles

from .compile import chapter_source_pages, validate_semantics
from .source import evidence_pages
from .store import NeedsAttention, digest

FIELDS = {"goals": "goal", "events": "event", "new_entities": "entity", "new_secrets": "secret"}


def _rows(spine, bundles):
    rows = []
    for field, kind in (("entities", "entity"), ("secrets", "secret")):
        rows.extend((None, field, i, kind, row) for i, row in enumerate(spine[field]))
    for chapter in bundles["chapters"]:
        for field, kind in FIELDS.items():
            rows.extend(
                (chapter["chapter_id"], field, i, kind, row) for i, row in enumerate(chapter[field])
            )
    return rows


def structural_defects(spine, bundles):
    """Inspect raw typed rows before a registry could discard a colliding identity."""
    rows = _rows(spine, bundles)
    by_id = defaultdict(list)
    for row in rows:
        by_id[row[4]["id"]].append(row)
    permissions = defaultdict(set)
    issues, missing = [], set()

    def allow(chapter, field, index, *keys):
        permissions[(chapter, field, index)].update(keys)

    def issue(kind, chapter, field, index, **details):
        issues.append(
            {"issue": kind, "chapter_id": chapter, "field": field, "index": index, **details}
        )

    def consumers(identity, kind):
        for chapter in bundles["chapters"]:
            cid = chapter["chapter_id"]
            for i, goal in enumerate(chapter["goals"]):
                field = "event_ids" if kind == "event" else "depends_on" if kind == "goal" else None
                if field and identity in goal[field]:
                    allow(cid, "goals", i, field)
            for i, event in enumerate(chapter["events"]):
                keys = {
                    "event": ["next_event_ids"],
                    "goal": ["goal_ids"],
                    "entity": ["location_id", "participants", "clue_ids", "visible_entity_ids"],
                    "secret": ["secret_ids"],
                }[kind]
                for field in keys:
                    if identity == event.get(field) or identity in event.get(field, []):
                        allow(cid, "events", i, field)
            if kind == "event":
                for field in ("items", "checks"):
                    for i, row in enumerate(chapter.get(field, [])):
                        if identity in row["event_ids"]:
                            allow(cid, field, i, "event_ids")

    for identity, occurrences in sorted(by_id.items()):
        unique = {(kind, digest(row)) for _, _, _, kind, row in occurrences}
        if len(unique) < 2:
            continue
        # Canonical spine entities win; otherwise keep entities before event/goal IDs.
        ordered = sorted(
            occurrences, key=lambda r: (r[0] is not None, r[3] != "entity", r[3] != "secret")
        )
        canonical = ordered[0]
        for chapter, field, index, kind, row in ordered[1:]:
            if kind == canonical[3] and row == canonical[4]:
                continue
            if chapter is None or kind in ("entity", "secret"):
                raise NeedsAttention(
                    "Canonical identity conflict needs explicit reconciliation: " + identity
                )
            issue(
                "identity_collision",
                chapter,
                field,
                index,
                identity=identity,
                canonical_kind=canonical[3],
            )
            allow(chapter, field, index, "id")
            consumers(identity, kind)

    events = {row[4]["id"] for row in rows if row[3] == "event"}
    entities = {row[4]["id"]: row[4] for row in rows if row[3] == "entity"}
    for chapter in bundles["chapters"]:
        cid = chapter["chapter_id"]
        for i, event in enumerate(chapter["events"]):
            unknown = sorted(set(event["next_event_ids"]) - events)
            if unknown:
                issue("dangling_next_event", cid, "events", i, identities=unknown)
                allow(cid, "events", i, "next_event_ids")
            for encounter in event["encounters"]:
                actor = encounter["actor_id"]
                if actor not in entities or entities[actor]["kind"] != "npc":
                    issue("missing_encounter_actor", cid, "events", i, identity=actor)
                    allow(cid, "events", i, "encounters", "participants")
                    if actor not in entities:
                        missing.add((cid, actor))
                elif actor not in event["participants"]:
                    issue("encounter_outside_participants", cid, "events", i, identity=actor)
                    allow(cid, "events", i, "participants")
            invalid_reveals = [
                r["entity_id"]
                for r in event.get("reveals", [])
                if r["entity_id"] not in event["participants"] + event["clue_ids"]
            ]
            if invalid_reveals:
                issue("invalid_reveal_reference", cid, "events", i, identities=invalid_reveals)
                allow(cid, "events", i, "reveals", "participants", "clue_ids", "dm_notes")
    return issues, permissions, missing


def _check_scope(original, replacement, permissions, missing):
    """Preserve all unrelated contained rows, source prose, origins and mechanics."""
    old = {b["chapter_id"]: b for b in original["chapters"]}
    changed = {b["chapter_id"]: b for b in replacement["chapters"]}
    expected = {key[0] for key in permissions}
    if set(changed) != expected or len(changed) != len(replacement["chapters"]):
        raise NeedsAttention("Structural repair changed affected chapter inventory.")
    for cid, candidate in changed.items():
        chapter = old[cid]
        for field, before in chapter.items():
            after = candidate[field]
            if field not in (*FIELDS, "items", "checks"):
                if after != before:
                    raise NeedsAttention(
                        "Structural repair changed unrelated chapter content: " + cid + "." + field
                    )
                continue
            additions = after[len(before) :]
            if len(after) < len(before) or (additions and field != "new_entities"):
                raise NeedsAttention("Structural repair changed contained row inventory.")
            allowed_additions = {identity for owner, identity in missing if owner == cid}
            if any(row["id"] not in allowed_additions or row["kind"] != "npc" for row in additions):
                raise NeedsAttention("Structural repair added an unrelated entity.")
            for i, (previous, current) in enumerate(zip(before, after, strict=False)):
                allowed = permissions.get((cid, field, i), set())
                for key in previous.keys() | current.keys():
                    if previous.get(key) != current.get(key) and key not in allowed:
                        raise NeedsAttention(
                            "Structural repair changed unaffected row field: "
                            + cid
                            + "."
                            + field
                            + "."
                            + key
                        )
                if "dm_notes" in allowed and not current["dm_notes"].startswith(
                    previous["dm_notes"]
                ):
                    raise NeedsAttention("Structural repair replaced accepted discovery guidance.")
                if "reveals" in allowed:
                    invalid = [
                        r
                        for r in previous.get("reveals", [])
                        if r["entity_id"] not in previous["participants"] + previous["clue_ids"]
                    ]
                    valid = [r for r in previous.get("reveals", []) if r not in invalid]
                    if any(r not in current["reveals"] for r in valid):
                        raise NeedsAttention("Structural repair changed an unaffected reveal.")
                    for reveal in invalid:
                        retained = any(
                            all(row[k] == reveal[k] for k in ("public_text", "condition"))
                            for row in current["reveals"]
                        )
                        if not retained and any(
                            reveal[k] not in current["dm_notes"]
                            for k in ("public_text", "condition")
                        ):
                            raise NeedsAttention("Structural repair lost accepted discovery text.")
                    if any(
                        not any(
                            all(row[k] == old[k] for k in ("public_text", "condition"))
                            for old in previous.get("reveals", [])
                        )
                        for row in current["reveals"]
                    ):
                        raise NeedsAttention("Structural repair invented discovery text.")
                if "encounters" in allowed:
                    if len(previous["encounters"]) != len(current["encounters"]) or any(
                        {k: v for k, v in a.items() if k != "actor_id"}
                        != {k: v for k, v in b.items() if k != "actor_id"}
                        for a, b in zip(previous["encounters"], current["encounters"], strict=False)
                    ):
                        raise NeedsAttention(
                            "Structural repair changed accepted encounter mechanics."
                        )


async def repair_structure(workflow):
    """Return False on valid data, or True after accepting one scoped recovery.

    Saved failed candidates hold safely on restart. Provider/network interruptions
    replay the same durable request rather than creating a second paid submission.
    """
    store = workflow.store
    spine, bundles, source = store.get("spine"), store.get("bundles"), store.get("source")
    issues, permissions, missing = structural_defects(spine, bundles)
    if not issues:
        return False
    signature = digest({"spine": spine, "bundles": bundles, "issues": issues})
    history_key = "structural-repair-" + signature
    prior = store.get(history_key)
    if prior and prior.get("state") == "rejected":
        raise NeedsAttention(
            "Retained structural repair needs attention; unchanged defects were not resubmitted: "
            + prior["error"]
        )
    affected = {key[0] for key in permissions}
    page_numbers = sorted(
        {
            number
            for chapter in spine["chapters"]
            if chapter["id"] in affected
            for number in chapter_source_pages(spine, chapter)
        }
    )
    evidence = {
        "defects": issues,
        "allowed_fields": [
            {"chapter_id": cid, "field": field, "index": index, "keys": sorted(keys)}
            for (cid, field, index), keys in sorted(permissions.items())
        ],
        "allowed_new_actors": [
            {"chapter_id": cid, "id": identity} for cid, identity in sorted(missing)
        ],
        "chapters": [b for b in bundles["chapters"] if b["chapter_id"] in affected],
        "spine": spine,
        "all_event_ids": sorted({e["id"] for b in bundles["chapters"] for e in b["events"]}),
        "source_evidence": evidence_pages(source, page_numbers),
        "preferences": store.state().get("request", {}).get("preferences", ""),
    }
    if prior is None:
        store.put(
            history_key, {"state": "requested", "original_bundles": bundles, "defects": issues}
        )
    result = await workflow.semantic.call(
        "structural-repair",
        "structural-repair",
        Bundles,
        evidence,
        workflow.source_images(page_numbers),
    )
    replacement = result.model_dump()
    try:
        _check_scope(bundles, replacement, permissions, missing)
        by_chapter = {b["chapter_id"]: b for b in replacement["chapters"]}
        candidate = deepcopy(bundles)
        candidate["chapters"] = [by_chapter.get(b["chapter_id"], b) for b in candidate["chapters"]]
        validate_semantics(spine, candidate, source)
        if digest(candidate) == digest(bundles):
            raise NeedsAttention("Structural repair made no progress.")
    except (ValueError, KeyError) as exc:
        store.put(
            history_key,
            {
                "state": "rejected",
                "original_bundles": bundles,
                "candidate": replacement,
                "defects": issues,
                "error": str(exc),
            },
        )
        raise NeedsAttention("Retained structural repair was rejected: " + str(exc)) from exc
    # Immutable original and paid response remain available after the derived aggregate changes.
    store.put(
        history_key,
        {
            "state": "accepted",
            "original_bundles": bundles,
            "candidate": replacement,
            "result_sha256": digest(candidate),
            "defects": issues,
        },
    )
    store.put("bundles", candidate, digest(spine))
    return True
