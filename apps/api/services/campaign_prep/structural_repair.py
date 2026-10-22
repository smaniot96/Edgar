"""Recover invalid typed joins without reauthoring accepted chapter content.

The ordinary path makes no semantic call. Defective bundles receive one scoped
repair with their original evidence; its complete candidate is checked atomically.
"""

from __future__ import annotations

import base64
import json
from collections import defaultdict
from copy import deepcopy
from typing import Literal

from pydantic import Field

from edgar_core.campaign_semantics import SCHEMA_VERSION, Bundles, Entity, Record
from edgar_core.identifiers import Identifier

from .compile import chapter_source_pages, validate_semantics
from .profile_references import (
    PROFILE_REFERENCE_POLICY,
    ProfileCorrectionRejected,
    profile_reference_defects,
    reconcile_profile_pages,
)
from .provider import EFFORT, MODEL, OutputExhausted, instructions, response_format, response_text
from .secret_references import (
    SECRET_REFERENCE_POLICY,
    SecretCorrectionRejected,
    chapter_reference_defects,
    reconcile_secret_chapters,
)
from .source import evidence_pages
from .store import NeedsAttention, digest

FIELDS = {"goals": "goal", "events": "event", "new_entities": "entity", "new_secrets": "secret"}
RECOVERY_POLICY = "sparse-reference-joins-retain-content-5"
REFERENCE_RESIDUAL_POLICY = "scoped-retained-reference-residuals-1"


class ScalarJoin(Record):
    key: Literal["id", "location_id"]
    value: Identifier


class ListJoin(Record):
    key: Literal[
        "event_ids",
        "depends_on",
        "next_event_ids",
        "goal_ids",
        "participants",
        "clue_ids",
        "visible_entity_ids",
        "secret_ids",
    ]
    value: list[Identifier]


class ActorJoin(Record):
    index: int = Field(ge=0)
    actor_id: Identifier


class RevealJoin(Record):
    index: int = Field(ge=0)
    entity_id: Identifier | None = Field(
        description="Existing local participant/clue, or null to retain the original as DM guidance."
    )


class RowJoins(Record):
    chapter_id: Identifier
    field: Literal["goals", "events", "items", "checks"]
    index: int = Field(ge=0)
    expected_id: Identifier
    changes: list[ScalarJoin | ListJoin] = Field(default_factory=list)
    actors: list[ActorJoin] = Field(default_factory=list)
    reveals: list[RevealJoin] = Field(default_factory=list)


class NewActor(Record):
    chapter_id: Identifier
    entity: Entity


class StructuralJoins(Record):
    rows: list[RowJoins] = Field(default_factory=list)
    new_actors: list[NewActor] = Field(default_factory=list)


class ItemJoin(Record):
    chapter_id: Identifier
    index: int = Field(ge=0)
    expected_id: Identifier
    entity_id: Identifier


class StructuralLinks(Record):
    rows: list[RowJoins] = Field(default_factory=list)
    items: list[ItemJoin] = Field(default_factory=list)
    new_entities: list[NewActor] = Field(default_factory=list)


def _allowed_kinds(missing, key):
    return missing[key] if isinstance(missing, dict) else {"npc"}


def _sparse_replacement(original, patch, permissions, missing):
    """Reconstruct proposed rows locally; no accepted prose is model output."""
    patch = StructuralJoins.model_validate(patch)
    originals = {b["chapter_id"]: b for b in original["chapters"]}
    chapters = {
        b["chapter_id"]: deepcopy(b)
        for b in original["chapters"]
        if b["chapter_id"] in {key[0] for key in permissions}
    }
    seen = set()
    for edit in patch.rows:
        key = edit.chapter_id, edit.field, edit.index
        if key in seen or key not in permissions:
            raise NeedsAttention("Structural join patch repeated or targeted an unaffected row.")
        seen.add(key)
        chapter = chapters[edit.chapter_id]
        if edit.index >= len(chapter[edit.field]):
            raise NeedsAttention("Structural join patch row index is out of bounds.")
        row = chapter[edit.field][edit.index]
        identity_key = "entity_id" if edit.field == "items" else "id"
        if row[identity_key] != edit.expected_id:
            raise NeedsAttention("Structural join patch does not match the original row identity.")
        keys = set()
        for change in edit.changes:
            if change.key in keys or change.key not in permissions[key]:
                raise NeedsAttention(
                    "Structural join patch changed an unpermitted or repeated key."
                )
            keys.add(change.key)
            row[change.key] = deepcopy(change.value)
        for name, edits in (("encounters", edit.actors), ("reveals", edit.reveals)):
            indexes = set()
            if edits and (edit.field != "events" or name not in permissions[key]):
                raise NeedsAttention("Structural join patch changed an unpermitted nested join.")
            removed = set()
            for nested in edits:
                if nested.index in indexes or nested.index >= len(row[name]):
                    raise NeedsAttention(
                        "Structural join patch nested index is invalid or repeated."
                    )
                indexes.add(nested.index)
                target = row[name][nested.index]
                if name == "encounters":
                    target["actor_id"] = nested.actor_id
                else:
                    old = originals[edit.chapter_id]["events"][edit.index]
                    if target["entity_id"] in old["participants"] + old["clue_ids"]:
                        raise NeedsAttention("Structural join patch changed an unaffected reveal.")
                    if nested.entity_id is None:
                        removed.add(nested.index)
                    else:
                        target["entity_id"] = nested.entity_id
            if removed:
                row[name] = [value for i, value in enumerate(row[name]) if i not in removed]
    added = set()
    for addition in patch.new_actors:
        key = addition.chapter_id, addition.entity.id
        if key in added or key not in missing or addition.entity.kind != "npc":
            raise NeedsAttention("Structural join patch added an unrelated or repeated actor.")
        added.add(key)
        chapters[addition.chapter_id]["new_entities"].append(addition.entity.model_dump())
    return {"chapters": list(chapters.values())}


def _legacy_replacement(original, replacement, permissions, missing):
    """Salvage only unambiguous permitted joins from a paid full-bundle response.

    Old providers sometimes omitted chapters and returned only a subset of rows.
    Match stable identities, never their new positions or guessed renamed IDs.
    Missing rows remain original and become explicit residual sparse work.
    """
    affected = {key[0] for key in permissions}
    identities = {
        "event": {r["id"] for b in original["chapters"] for r in b["events"]},
        "goal": {r["id"] for b in original["chapters"] for r in b["goals"]},
    }
    proposals = defaultdict(list)
    for chapter in replacement["chapters"]:
        proposals[chapter["chapter_id"]].append(chapter)
    result = []
    for original_chapter in original["chapters"]:
        cid = original_chapter["chapter_id"]
        if cid not in affected:
            continue
        chapter = deepcopy(original_chapter)
        if len(proposals[cid]) != 1:
            result.append(chapter)
            continue
        proposal = proposals[cid][0]
        for field in (*FIELDS, "items", "checks"):
            identity_key = "entity_id" if field == "items" else "id"
            rows = defaultdict(list)
            for row in proposal[field]:
                rows[row[identity_key]].append(row)
            for index, row in enumerate(chapter[field]):
                matches = rows[row[identity_key]]
                if len(matches) != 1:
                    continue
                proposed = matches[0]
                for key in permissions.get((cid, field, index), set()) - {"id", "dm_notes"}:
                    kind = (
                        "event"
                        if key in ("event_ids", "next_event_ids")
                        else "goal"
                        if key in ("goal_ids", "depends_on")
                        else None
                    )
                    if kind and set(proposed[key]) - identities[kind]:
                        # A renamed row omitted by a legacy subset cannot justify
                        # redirecting its consumers to an unaccepted new identity.
                        continue
                    if key == "encounters":
                        if len(row[key]) != len(proposed[key]):
                            continue
                        for accepted, candidate in zip(row[key], proposed[key], strict=True):
                            accepted["actor_id"] = candidate["actor_id"]
                    else:
                        row[key] = deepcopy(proposed[key])
            if field == "new_entities":
                existing = {row["id"] for row in chapter[field]}
                for owner, identity in sorted(missing):
                    matches = rows[identity]
                    if (
                        owner == cid
                        and identity not in existing
                        and len(matches) == 1
                        and matches[0]["kind"] == "npc"
                    ):
                        chapter[field].append(deepcopy(matches[0]))
        result.append(chapter)
    return {"chapters": result}


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


def _project_joins(original, replacement, permissions, missing):
    """Apply permitted joins to original content; never ask a model to copy prose.

    Chapter and row inventories remain strict. Within an existing row only the
    diagnosed fields are proposals; all other output is discarded, including
    accidental paraphrases and encounter changes other than actor identity.
    """
    old = {b["chapter_id"]: b for b in original["chapters"]}
    changed = {b["chapter_id"]: b for b in replacement["chapters"]}
    if set(changed) != {key[0] for key in permissions} or len(changed) != len(
        replacement["chapters"]
    ):
        raise NeedsAttention("Structural repair changed affected chapter inventory.")
    result = {**deepcopy(original), "chapters": []}
    for cid, proposal in changed.items():
        chapter = deepcopy(old[cid])
        for field in (*FIELDS, "items", "checks"):
            before, after = chapter[field], proposal[field]
            additions = after[len(before) :]
            if len(after) < len(before) or (additions and field != "new_entities"):
                raise NeedsAttention("Structural repair changed contained row inventory.")
            if len({row["id"] for row in additions}) != len(additions) or any(
                (cid, row["id"]) not in missing
                or row["kind"] not in _allowed_kinds(missing, (cid, row["id"]))
                for row in additions
            ):
                raise NeedsAttention("Structural repair added an unrelated entity.")
            for i, (previous, proposed) in enumerate(zip(before, after, strict=False)):
                allowed = permissions.get((cid, field, i), set())
                # Preserve positional identity, except for an explicitly diagnosed collision.
                identity_key = "entity_id" if field == "items" else "id"
                if identity_key not in allowed and previous[identity_key] != proposed[identity_key]:
                    raise NeedsAttention("Structural repair reordered contained rows.")
                for key in allowed:
                    if key == "encounters":
                        if len(previous[key]) != len(proposed[key]):
                            raise NeedsAttention("Structural repair changed encounter inventory.")
                        for accepted, candidate in zip(previous[key], proposed[key], strict=True):
                            accepted["actor_id"] = candidate["actor_id"]
                    else:
                        previous[key] = deepcopy(proposed[key])
            before.extend(deepcopy(additions))
        result["chapters"].append(chapter)
    return result


def _retain_discoveries(original, replacement, permissions):
    """Copy accepted discovery wording locally instead of paying a model to copy it.

    A typed join may move a remote discovery to private guidance. Its exact text and
    condition are already accepted content, so paraphrasing them adds no useful work.
    The raw paid proposal remains in its immutable provider receipt.
    """
    value = deepcopy(replacement)
    old = {b["chapter_id"]: b for b in original["chapters"]}
    for chapter in value["chapters"]:
        if chapter["chapter_id"] not in old:
            continue
        cid = chapter["chapter_id"]
        for i, (previous, current) in enumerate(
            zip(old[cid]["events"], chapter["events"], strict=False)
        ):
            allowed = permissions.get((cid, "events", i), set())
            if "reveals" not in allowed:
                current["reveals"] = deepcopy(previous.get("reveals", []))
                continue
            invalid = [
                r
                for r in previous.get("reveals", [])
                if r["entity_id"] not in previous["participants"] + previous["clue_ids"]
            ]
            valid = [r for r in previous.get("reveals", []) if r not in invalid]
            # Preserve already valid reveals verbatim. Only retarget an invalid one
            # when the proposal retains its exact text and condition.
            retargeted = [
                row
                for row in current["reveals"]
                if any(all(row[k] == r[k] for k in ("public_text", "condition")) for r in invalid)
            ]
            current["reveals"] = deepcopy(valid) + retargeted
            current["dm_notes"] = previous["dm_notes"]
            for reveal in invalid:
                if not any(
                    all(row[k] == reveal[k] for k in ("public_text", "condition"))
                    for row in retargeted
                ):
                    current["dm_notes"] += (
                        "\n\nDiscovery guidance (retained original):\nCondition: "
                        + reveal["condition"]
                        + "\nObservation: "
                        + reveal["public_text"]
                    )
    return value


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
            if any(
                (cid, row["id"]) not in missing
                or row["kind"] not in _allowed_kinds(missing, (cid, row["id"]))
                for row in additions
            ):
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


def _repair_evidence(workflow, spine, bundles, source, issues, permissions, missing):
    affected = {key[0] for key in permissions}
    page_numbers = sorted(
        {
            number
            for chapter in spine["chapters"]
            if chapter["id"] in affected
            for number in chapter_source_pages(spine, chapter)
        }
    )
    return {
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
        "preferences": workflow.store.state().get("request", {}).get("preferences", ""),
    }, page_numbers


def _legacy_request(store, evidence):
    """An existing full-output dispatch must resolve before any sparse submission."""
    with store.db() as db:
        rows = list(db.execute("SELECT * FROM task WHERE node='structural-repair'"))
    matches = []
    for row in rows:
        request = json.loads(row["request"])
        for message in request.get("input", []):
            if message.get("role") == "user":
                for part in message.get("content", []):
                    if part.get("type") == "input_text" and json.loads(part["text"]) == evidence:
                        matches.append(dict(row))
    if len(matches) > 1:
        raise NeedsAttention("Multiple retained full structural requests need reconciliation.")
    return matches[0] if matches else None


def _merge_chapters(bundles, replacement):
    changed = {chapter["chapter_id"]: chapter for chapter in replacement["chapters"]}
    return {
        **deepcopy(bundles),
        "chapters": [changed.get(b["chapter_id"], b) for b in bundles["chapters"]],
    }


def _normalize_origins(spine, bundles, source):
    """Keep source section labels as prose; never fabricate an OCR-region join."""
    spine, bundles = deepcopy(spine), deepcopy(bundles)
    if source.get("kind") != "pdf":
        return spine, bundles, []
    pages = {p["page"]: {r["id"] for r in p["regions"]} for p in source.get("pages", [])}
    changes = []
    rows = [(kind, row) for _, _, _, kind, row in _rows(spine, bundles)]
    rows.extend(
        (field, row)
        for chapter in bundles["chapters"]
        for field in ("items", "checks")
        for row in chapter[field]
    )
    for kind, row in rows:
        origin = row["origin"]
        valid = set().union(*(pages.get(p, set()) for p in origin["pages"]))
        labels = [value for value in origin["regions"] if value not in valid]
        if labels:
            origin["regions"] = [value for value in origin["regions"] if value in valid]
            origin["note"] += (
                ("\n\n" if origin["note"] else "")
                + "Original source locator labels (not OCR region IDs): "
                + json.dumps(labels, ensure_ascii=False)
            )
            changes.append(
                {"kind": kind, "id": row.get("id", row.get("entity_id")), "retained_labels": labels}
            )
    return spine, bundles, changes


def reference_defects(spine, bundles):
    """Audit all executable reference collections before requesting residual work."""
    rows = _rows(spine, bundles)
    entities = {row["id"]: row for _, _, _, kind, row in rows if kind == "entity"}
    typed = {
        kind: {row["id"] for _, _, _, k, row in rows if k == kind}
        for kind in ("event", "goal", "secret")
    }
    issues, permissions, needed = [], defaultdict(set), {}

    def defect(cid, field, index, key, invalid, kinds=None):
        if not invalid:
            return
        issues.append(
            {
                "issue": "invalid_reference",
                "chapter_id": cid,
                "field": field,
                "index": index,
                "key": key,
                "identities": sorted(invalid),
            }
        )
        permissions[(cid, field, index)].add(key)
        if kinds:
            for identity in invalid:
                if identity in entities or any(identity in values for values in typed.values()):
                    continue
                owner, allowed = needed.get(identity, (cid, set(kinds)))
                needed[identity] = owner, allowed & set(kinds)

    all_kinds = {"npc", "place", "faction", "clue"}
    for chapter in bundles["chapters"]:
        cid = chapter["chapter_id"]
        local_events = {row["id"] for row in chapter["events"]}
        for index, goal in enumerate(chapter["goals"]):
            for key, kind in (("event_ids", "event"), ("depends_on", "goal")):
                defect(cid, "goals", index, key, set(goal[key]) - typed[kind])
        for index, event in enumerate(chapter["events"]):
            for key, kinds in (
                ("location_id", {"place"}),
                ("participants", all_kinds),
                ("clue_ids", {"clue"}),
                ("visible_entity_ids", all_kinds),
            ):
                values = [event[key]] if key == "location_id" else event[key]
                invalid = {
                    value
                    for value in values
                    if value not in entities or entities[value]["kind"] not in kinds
                }
                defect(cid, "events", index, key, invalid, kinds)
            for key, kind in (
                ("goal_ids", "goal"),
                ("secret_ids", "secret"),
                ("next_event_ids", "event"),
            ):
                defect(cid, "events", index, key, set(event[key]) - typed[kind])
        for field in ("items", "checks"):
            for index, row in enumerate(chapter[field]):
                defect(cid, field, index, "event_ids", set(row["event_ids"]) - local_events)
                if field != "items":
                    continue
                identity = row["entity_id"]
                if identity in entities and entities[identity]["kind"] == "clue":
                    continue
                proposed = (
                    identity
                    if identity not in entities
                    and not any(identity in values for values in typed.values())
                    else cid[:38] + "_item_" + digest(row["procedure"])[:16]
                )
                issues.append(
                    {
                        "issue": "invalid_item_entity",
                        "chapter_id": cid,
                        "field": field,
                        "index": index,
                        "identity": identity,
                        "permitted_object_id": proposed,
                    }
                )
                permissions[(cid, field, index)].add("entity_id")
                needed[proposed] = cid, {"clue"}
    missing = {(cid, identity): kinds for identity, (cid, kinds) in needed.items()}
    return issues, permissions, missing


def _reference_replacement(bundles, patch, permissions, missing):
    patch = StructuralLinks.model_validate(patch)
    replacement = _sparse_replacement(
        bundles, {"rows": [row.model_dump() for row in patch.rows]}, permissions, set()
    )
    chapters = {chapter["chapter_id"]: chapter for chapter in replacement["chapters"]}
    seen = set()
    for item in patch.items:
        key = item.chapter_id, "items", item.index
        if key in seen or "entity_id" not in permissions.get(key, set()):
            raise NeedsAttention("Item join patch targeted an unpermitted or repeated row.")
        seen.add(key)
        row = chapters[item.chapter_id]["items"][item.index]
        if row["entity_id"] != item.expected_id:
            raise NeedsAttention("Item join patch does not match the original identity.")
        row["entity_id"] = item.entity_id
    added = set()
    for addition in patch.new_entities:
        key = addition.chapter_id, addition.entity.id
        if key in added or key not in missing or addition.entity.kind not in missing[key]:
            raise NeedsAttention("Reference repair added an unrelated or wrongly typed entity.")
        added.add(key)
        chapters[addition.chapter_id]["new_entities"].append(addition.entity.model_dump())
    return replacement


def _residual_reference_evidence(workflow, spine, bundles, source, issues, permissions, missing):
    """Supply only unresolved rows and their original pages, never whole chapters."""
    chapters = {b["chapter_id"]: b for b in bundles["chapters"]}
    affected = [
        {
            "chapter_id": cid,
            "field": field,
            "index": index,
            "record": chapters[cid][field][index],
        }
        for cid, field, index in sorted(permissions)
    ]
    page_numbers = sorted(
        {number for row in affected for number in row["record"]["origin"]["pages"]}
    )
    return {
        "defects": issues,
        "allowed_fields": [
            {"chapter_id": cid, "field": field, "index": index, "keys": sorted(keys)}
            for (cid, field, index), keys in sorted(permissions.items())
        ],
        "allowed_new_entities": [
            {"chapter_id": cid, "id": identity, "kinds": sorted(kinds)}
            for (cid, identity), kinds in sorted(missing.items())
        ],
        "affected_rows": affected,
        "existing_identities": [
            {
                "id": row["id"],
                "kind": row.get("kind", kind),
                "name": row.get("name", row.get("title", row["id"])),
            }
            for _, _, _, kind, row in _rows(spine, bundles)
        ],
        "source_evidence": evidence_pages(source, page_numbers),
        "preferences": workflow.store.state().get("request", {}).get("preferences", ""),
    }, page_numbers


async def _repair_reference_residuals(workflow, spine, bundles, source, history, history_key):
    """Continue incomplete paid joins with a separately retained, residual-only request.

    Each exact residual basis has one durable provider task. An unchanged or cycling
    result holds for inspection; there is no call-count cap or full-source replay.
    """
    entries = history.setdefault("reference_residuals", [])
    visited = set()
    while True:
        issues, permissions, missing = reference_defects(spine, bundles)
        if not issues:
            return bundles
        signature = digest({"bundles": bundles, "issues": issues})
        if signature in visited:
            raise NeedsAttention("Reference residual repair cycled without resolving its joins.")
        visited.add(signature)
        entry = next((r for r in entries if r["signature"] == signature), None)
        if entry is None:
            entry = {"signature": signature, "defects": issues, "state": "requested"}
            entries.append(entry)
        if entry["state"] == "no_progress":
            raise NeedsAttention("Retained reference residual made no progress; not resubmitted.")
        evidence, page_numbers = _residual_reference_evidence(
            workflow, spine, bundles, source, issues, permissions, missing
        )
        entry["evidence_sha256"] = digest(evidence)
        workflow.store.put(history_key, history)
        raw = entry.get("candidate")
        if raw is None:
            result = await workflow.semantic.call(
                "structural-links-residual-" + signature,
                "structural-links",
                StructuralLinks,
                evidence,
                workflow.source_images(page_numbers),
            )
            raw = result.model_dump()
            entry["candidate"] = raw
            workflow.store.put(history_key, history)
        replacement = _reference_replacement(bundles, raw, permissions, missing)
        replacement = _project_joins(bundles, replacement, permissions, missing)
        _check_scope(bundles, replacement, permissions, missing)
        candidate = _merge_chapters(bundles, replacement)
        if digest(candidate) == digest(bundles):
            entry["state"] = "no_progress"
            workflow.store.put(history_key, history)
            raise NeedsAttention("Reference residual repair made no progress; not resubmitted.")
        entry.update(state="projected", result_sha256=digest(candidate))
        workflow.store.put(history_key, history)
        bundles = candidate


async def _repair_references(workflow, spine, bundles, source, history, prior, history_key):
    issues, permissions, missing = reference_defects(spine, bundles)
    if not issues:
        return bundles
    # Reuse exact canonical entities from a retained full paid candidate. An omitted
    # identity or disagreement stays explicit semantic work, never a guessed entity.
    retained = defaultdict(list)
    for chapter in (history.get("legacy_candidate") or {}).get("chapters", []):
        for row in chapter["new_entities"]:
            if row not in retained[row["id"]]:
                retained[row["id"]].append(row)
    recovered = []
    basis = deepcopy(bundles)
    chapters = {b["chapter_id"]: b for b in basis["chapters"]}
    for (cid, identity), kinds in missing.items():
        values = retained[identity]
        if len(values) == 1 and values[0]["kind"] in kinds:
            chapters[cid]["new_entities"].append(Entity.model_validate(values[0]).model_dump())
            recovered.append(identity)
    if recovered:
        spine, basis, labels = _normalize_origins(spine, basis, source)
        history["recovered_reference_entities"] = recovered
        history.setdefault("origin_normalizations", []).extend(labels)
        issues, permissions, missing = reference_defects(spine, basis)
    if not issues:
        return basis
    evidence, page_numbers = _repair_evidence(
        workflow, spine, basis, source, issues, permissions, set()
    )
    evidence["allowed_new_entities"] = [
        {"chapter_id": cid, "id": identity, "kinds": sorted(kinds)}
        for (cid, identity), kinds in sorted(missing.items())
    ]
    history.update(reference_defects=issues, reference_basis=basis)
    workflow.store.put(history_key, history)
    raw = prior.get("reference_candidate")
    if raw is None:
        result = await workflow.semantic.call(
            "structural-links",
            "structural-links",
            StructuralLinks,
            evidence,
            workflow.source_images(page_numbers),
        )
        raw = result.model_dump()
    history["reference_candidate"] = raw
    workflow.store.put(history_key, history)
    replacement = _reference_replacement(basis, raw, permissions, missing)
    replacement = _project_joins(basis, replacement, permissions, missing)
    _check_scope(basis, replacement, permissions, missing)
    return await _repair_reference_residuals(
        workflow, spine, _merge_chapters(basis, replacement), source, history, history_key
    )


async def repair_structure(workflow):
    """Repair joins with sparse output, retaining every already-paid full response.

    An old incomplete/ambiguous dispatch holds through its original provider path.
    Completed subset output is projected by stable identity; a distinct sparse call
    handles only its actual residual defects. Full validation gates atomic acceptance.
    """
    store = workflow.store
    spine, bundles, source = store.get("spine"), store.get("bundles"), store.get("source")
    issues, permissions, missing = structural_defects(spine, bundles)
    refs, _, _ = reference_defects(spine, bundles)
    secret_refs = chapter_reference_defects(spine, bundles)
    profile_refs = profile_reference_defects(bundles, source)
    _, _, labels = _normalize_origins(spine, bundles, source)
    if not issues and not refs and not labels and not secret_refs and not profile_refs:
        return False
    signature = digest({"spine": spine, "bundles": bundles, "issues": issues})
    history_key = "structural-repair-" + signature
    prior = store.get(history_key) or {}
    if (
        prior.get("state") == "rejected"
        and prior.get("policy") == RECOVERY_POLICY
        and not (
            prior.get("reference_candidate") is not None
            and prior.get("reference_residual_policy") != REFERENCE_RESIDUAL_POLICY
        )
        and not (secret_refs and prior.get("secret_reference_policy") != SECRET_REFERENCE_POLICY)
        and not (profile_refs and prior.get("profile_reference_policy") != PROFILE_REFERENCE_POLICY)
    ):
        raise NeedsAttention(
            "Retained structural repair needs attention; unchanged defects were not resubmitted: "
            + prior["error"]
        )
    raw = prior.get("raw_candidate") or prior.get("candidate") or {}
    legacy = prior.get("legacy_candidate") or (raw if "chapters" in raw else None)
    sparse = prior.get("sparse_candidate") or (raw if "rows" in raw else None)
    evidence, page_numbers = _repair_evidence(
        workflow, spine, bundles, source, issues, permissions, missing
    )
    history = {
        "state": "requested",
        "policy": RECOVERY_POLICY,
        "original_bundles": bundles,
        "original_spine": spine,
        "defects": issues,
        "reference_residual_policy": REFERENCE_RESIDUAL_POLICY,
        "secret_reference_policy": SECRET_REFERENCE_POLICY,
        "profile_reference_policy": PROFILE_REFERENCE_POLICY,
        "reference_residuals": deepcopy(prior.get("reference_residuals", [])),
    }
    store.put(history_key, history)
    retained = _legacy_request(store, evidence) if legacy is None else None
    if retained is not None:
        # Preserve the old prompt/schema/request identity, including terminal incomplete
        # output. Never launch a sparse alternative for an unresolved paid dispatch.
        response = json.loads(retained["response"]) if retained["response"] else {}
        if retained["accepted"]:
            legacy = Bundles.model_validate_json(retained["accepted"]).model_dump()
        elif response.get("status") == "completed":
            legacy = Bundles.model_validate_json(response_text(response)).model_dump()
        elif response.get("status") == "incomplete":
            raise OutputExhausted(retained["id"], response)
        else:
            if response.get("status") not in ("queued", "in_progress"):
                raise NeedsAttention("Retained structural dispatch needs response reconciliation.")
            saved = json.loads(retained["request"])
            images = workflow.source_images(page_numbers)
            parts = [
                {
                    "type": "input_text",
                    "text": json.dumps(evidence, ensure_ascii=False, sort_keys=True),
                }
            ]
            parts.extend(
                {
                    "type": "input_image",
                    "image_url": "data:image/png;base64,"
                    + base64.b64encode(path.read_bytes()).decode(),
                    "detail": "high",
                }
                for path in images
            )
            expected = {
                "model": MODEL,
                "reasoning": {"effort": EFFORT},
                "input": [
                    {"role": "system", "content": instructions("structural-repair")},
                    {"role": "user", "content": parts},
                ],
                "text": {"format": response_format(Bundles)},
                "background": True,
                "store": True,
                "service_tier": "default",
            }
            comparable = deepcopy(saved)
            comparable["input"][0] = expected["input"][0]
            if comparable != expected or retained["id"] != digest(
                {
                    "node": "structural-repair",
                    "request": saved,
                    "schema_version": SCHEMA_VERSION,
                }
            ):
                raise NeedsAttention("Retained structural request needs schema reconciliation.")
            result = await workflow.semantic.call(
                "structural-repair",
                "structural-repair",
                Bundles,
                evidence,
                images,
                reuse_saved_request=True,
            )
            legacy = result.model_dump()
    basis = bundles
    awaiting_response = False
    try:
        if legacy is not None:
            history["legacy_candidate"] = legacy
            history["raw_candidate"] = legacy
            replacement = _legacy_replacement(bundles, legacy, permissions, missing)
            replacement = _project_joins(bundles, replacement, permissions, missing)
            replacement = _retain_discoveries(bundles, replacement, permissions)
            _check_scope(bundles, replacement, permissions, missing)
            basis = _merge_chapters(bundles, replacement)
        residual, allowed, absent = structural_defects(spine, basis)
        if residual:
            evidence, page_numbers = _repair_evidence(
                workflow, spine, basis, source, residual, allowed, absent
            )
            history.update(projected_bundles=basis, residual_defects=residual)
            store.put(history_key, history)
            if sparse is None:
                awaiting_response = True
                result = await workflow.semantic.call(
                    "structural-joins",
                    "structural-joins",
                    StructuralJoins,
                    evidence,
                    workflow.source_images(page_numbers),
                )
                awaiting_response = False
                sparse = result.model_dump()
            history["sparse_candidate"] = sparse
            if legacy is None:
                history["raw_candidate"] = sparse
            replacement = _sparse_replacement(basis, sparse, allowed, absent)
            replacement = _project_joins(basis, replacement, allowed, absent)
            replacement = _retain_discoveries(basis, replacement, allowed)
            _check_scope(basis, replacement, allowed, absent)
            candidate = _merge_chapters(basis, replacement)
        else:
            candidate = basis
        if structural_defects(spine, candidate)[0]:
            raise NeedsAttention("Structural repair left its diagnosed joins unresolved.")
        normalized_spine, candidate, labels = _normalize_origins(spine, candidate, source)
        history["origin_normalizations"] = labels
        awaiting_response = True
        candidate = await _repair_references(
            workflow, normalized_spine, candidate, source, history, prior, history_key
        )
        awaiting_response = False
        normalized_spine, candidate, new_labels = _normalize_origins(
            normalized_spine, candidate, source
        )
        history["origin_normalizations"].extend(new_labels)
        awaiting_response = True
        try:
            normalized_spine, candidate = await reconcile_secret_chapters(
                workflow, normalized_spine, candidate
            )
        except SecretCorrectionRejected:
            awaiting_response = False
            raise
        try:
            candidate = await reconcile_profile_pages(workflow, normalized_spine, candidate)
        except ProfileCorrectionRejected:
            awaiting_response = False
            raise
        awaiting_response = False
        validate_semantics(normalized_spine, candidate, source)
        if digest(candidate) == digest(bundles) and digest(normalized_spine) == digest(spine):
            raise NeedsAttention("Structural repair made no progress.")
    except (ValueError, KeyError) as exc:
        if awaiting_response:
            # The provider owns its durable receipt. Re-enter the same request after
            # reconciliation, rather than rejecting it or dispatching another format.
            raise
        history.update(state="rejected", error=str(exc))
        store.put(history_key, history)
        raise NeedsAttention("Retained structural repair was rejected: " + str(exc)) from exc
    history.update(state="accepted", candidate=candidate, result_sha256=digest(candidate))
    store.put(history_key, history)
    original_events = {
        event["id"]: event for bundle in bundles["chapters"] for event in bundle["events"]
    }
    corrections = list(store.get("structural-progression-corrections") or [])
    for bundle in candidate["chapters"]:
        for event in bundle["events"]:
            original = original_events.get(event["id"])
            if original and original["next_event_ids"] != event["next_event_ids"]:
                correction = {
                    "event_id": event["id"],
                    "original_next_event_ids": original["next_event_ids"],
                    "repaired_next_event_ids": event["next_event_ids"],
                    "repair_receipt": history_key,
                }
                if correction not in corrections:
                    corrections.append(correction)
    if corrections:
        store.put("structural-progression-corrections", corrections)
    if normalized_spine != spine:
        store.put("spine", normalized_spine)
    store.put("bundles", candidate, digest(normalized_spine))
    return True
