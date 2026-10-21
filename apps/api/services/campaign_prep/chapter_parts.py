"""Incremental chapter authoring; a chapter need not fit in one provider output.

Original task receipts remain immutable. Closed records recovered from an exhausted
response are provisional inputs to a distinct completion task, never an accepted
provider response. Final chapter validation and independent review still apply.
"""

from __future__ import annotations

from copy import deepcopy

from pydantic import Field

from edgar_core.campaign_semantics import (
    Bundle,
    Bundles,
    Entity,
    Event,
    Goal,
    Record,
    Secret,
    SourceCheck,
    SourceItem,
)
from edgar_core.identifiers import Identifier

from .provider import OutputExhausted, completed_json_values, request_context
from .source import evidence_pages, token_count
from .store import NeedsAttention, digest

# A work-sizing target, not an output/request/cost limit. Large physical pages stay
# intact, and all source pages are eventually processed. Based on measured module
# authoring expansion, a few thousand source tokens produce a useful group of events.
SOURCE_TOKENS_PER_PART = 6_000
RECORD_TYPES = {
    "goals": Goal,
    "events": Event,
    "new_entities": Entity,
    "new_secrets": Secret,
    "items": SourceItem,
    "checks": SourceCheck,
}


class ChapterPart(Record):
    chapter_id: Identifier
    goals: list[Goal] = Field(default_factory=list)
    events: list[Event] = Field(default_factory=list)
    new_entities: list[Entity] = Field(default_factory=list)
    new_secrets: list[Secret] = Field(default_factory=list)
    items: list[SourceItem] = Field(default_factory=list)
    checks: list[SourceCheck] = Field(default_factory=list)
    dm_guidance: str = ""
    source_notes: str = ""
    source_pages: list[int] = Field(default_factory=list)
    complete: bool
    remaining_work: str = ""


def page_groups(spine, chapter, source):
    """Own disjoint core pages; supply shared appendices as reference to each group."""
    assigned = set(chapter["pages"])
    others = {
        page
        for other in spine["chapters"]
        if other["id"] != chapter["id"]
        for page in other["pages"]
    }
    core = assigned - others or assigned
    rows = evidence_pages(source, sorted(core))
    groups, current, size = [], [], 0
    for row in rows:
        amount = token_count(row["text"])
        if current and size + amount > SOURCE_TOKENS_PER_PART:
            groups.append(current)
            current, size = [], 0
        current.append(row["physical_page"])
        size += amount
    if current:
        groups.append(current)
    if len(groups) <= 1:
        return [sorted(assigned)]
    # Shared pages are reference-only in later packets, with one explicit owner.
    groups[0] = sorted(set(groups[0]) | (assigned - core))
    return groups


def blank(chapter_id):
    return {
        "chapter_id": chapter_id,
        **{key: [] for key in RECORD_TYPES},
        "dm_guidance": "",
        "source_notes": "",
        "source_pages": [],
    }


def record_key(field, row):
    return row["entity_id"] if field == "items" else row["id"]


def merge(target, addition):
    """Add records, never silently overwrite accepted or provisionally closed work."""
    if addition.get("chapter_id", target["chapter_id"]) != target["chapter_id"]:
        raise NeedsAttention("Chapter part belongs to a different chapter.")
    result = deepcopy(target)
    for field in RECORD_TYPES:
        existing = {record_key(field, row): row for row in result[field]}
        for row in addition.get(field, []):
            key = record_key(field, row)
            if key in existing:
                if existing[key] != row:
                    raise NeedsAttention("Chapter parts conflict on retained record " + key)
            else:
                result[field].append(deepcopy(row))
                existing[key] = row
    for field in ("dm_guidance", "source_notes"):
        text = addition.get(field, "").strip()
        if text and text not in result[field]:
            result[field] = "\n\n".join(filter(None, [result[field], text]))
    result["source_pages"] = sorted(
        set(result["source_pages"]) | set(addition.get("source_pages", []))
    )
    return result


def closed_content(exhausted, chapter_id, *, wrapped):
    """Retain only fully serialized fields/records; preserve unfinished fields separately."""
    values = completed_json_values(exhausted.output_text)
    result, pending = blank(chapter_id), []
    prefix = ["chapters", 0] if wrapped else []
    for entry in values:
        path, value = entry["path"], entry["value"]
        if not path:
            if wrapped and len(value.get("chapters", [])) != 1:
                raise NeedsAttention("Exhausted output does not contain exactly one chapter.")
            value = value["chapters"][0] if wrapped else value
            path = prefix
        elif wrapped and path == ["chapters"]:
            if len(value) != 1:
                raise NeedsAttention("Exhausted output does not contain exactly one chapter.")
            value, path = value[0], prefix
        if path == prefix:
            candidates = {k: v for k, v in value.items() if k in result}
        elif path[: len(prefix)] != prefix:
            continue
        else:
            tail = path[len(prefix) :]
            if len(tail) == 1 and tail[0] in result:
                candidates = {tail[0]: value}
            elif len(tail) == 2 and tail[0] in RECORD_TYPES and isinstance(tail[1], int):
                candidates = {tail[0]: [value]}
            else:
                pending.append(entry)
                continue
        for field, content in candidates.items():
            if field in RECORD_TYPES:
                for row in content:
                    parsed = RECORD_TYPES[field].model_validate(row).model_dump()
                    # A closed record with missing optional fields is not silently
                    # repaired with defaults during recovery.
                    if parsed != row:
                        raise NeedsAttention("Recovered record is not completely serialized.")
                    result = merge(result, {field: [row]})
            elif field == "chapter_id" and content != chapter_id:
                raise NeedsAttention("Exhausted response belongs to a different chapter.")
            elif field in ("source_pages", "source_notes", "dm_guidance"):
                result[field] = deepcopy(content)
    return result, pending


def unfinished_constraints(values):
    """Identify closed fields of an unfinished record by its supplied stable ID."""
    grouped = {}
    for entry in values:
        path = entry["path"]
        if path[:2] == ["chapters", 0]:
            path = path[2:]
        if len(path) >= 3 and path[0] in RECORD_TYPES and isinstance(path[1], int):
            grouped.setdefault(tuple(path[:2]), []).append(
                {"path": path[2:], "value": entry["value"]}
            )
    constraints = []
    for (field, _), entries in grouped.items():
        key = "entity_id" if field == "items" else "id"
        identity = next((e["value"] for e in entries if e["path"] == [key]), None)
        if identity is not None:
            constraints.append({"field": field, "id": identity, "closed_fields": entries})
    return constraints


def validate_constraints(bundle, constraints):
    for constraint in constraints:
        row = next(
            (
                value
                for value in bundle[constraint["field"]]
                if record_key(constraint["field"], value) == constraint["id"]
            ),
            None,
        )
        if row is None:
            raise NeedsAttention("Output recovery omitted unfinished record " + constraint["id"])
        for entry in constraint["closed_fields"]:
            value = row
            try:
                for key in entry["path"]:
                    value = value[key]
            except (KeyError, TypeError, IndexError) as exc:
                raise NeedsAttention("Recovered record lost a completed field.") from exc
            if value != entry["value"]:
                raise NeedsAttention("Recovered record changed a completed field.")


def has_original_task(store, chapter_id):
    with store.db() as db:
        return (
            db.execute(
                "SELECT 1 FROM task WHERE node=? LIMIT 1", ("chapter-" + chapter_id,)
            ).fetchone()
            is not None
        )


async def author_parts(workflow, evidence, *, exhausted=None, groups=None):
    """Sequential durable parts within a chapter; independent chapters remain parallel."""
    store = workflow.store
    chapter = evidence["requested_chapter"]
    chapter_id = chapter["id"]
    source = store.get("source")
    retained, pending, parents = blank(chapter_id), [], []
    constraints = []
    if exhausted is not None:
        retained, pending = closed_content(exhausted, chapter_id, wrapped=True)
        constraints.extend(unfinished_constraints(pending))
        parents.append(exhausted.task_id)
        store.put(
            "chapter-recovered-" + exhausted.task_id,
            {
                "parent_task_id": exhausted.task_id,
                "response_id": exhausted.response_id,
                "response_sha256": digest(exhausted.response),
                "provisional_records": retained,
                "unfinished_record_fields": pending,
            },
            digest(evidence),
        )
        # Fully closed serialized chapters can survive missing outer syntax. Derive
        # them without another paid call, keeping the incomplete task untouched.
        for entry in completed_json_values(exhausted.output_text):
            if entry["path"] not in ([], ["chapters"], ["chapters", 0]):
                continue
            value = entry["value"]
            if not entry["path"]:
                value = value["chapters"][0]
            elif entry["path"] == ["chapters"]:
                value = value[0]
            bundle = Bundle.model_validate(value).model_dump()
            if bundle != value or bundle["chapter_id"] != chapter_id:
                raise NeedsAttention("Closed exhausted chapter failed exact recovery.")
            supplied = set(evidence["source_context_pages"])
            if not set(chapter["pages"]) <= set(bundle["source_pages"]) <= supplied:
                raise NeedsAttention("Closed exhausted chapter lacks its source inventory.")
            return bundle, {"mode": "closed-output-recovery", "parent_task_ids": parents}
    groups = groups or [chapter["pages"]]
    map_context = workflow.map_context(chapter["pages"])
    map_pages = {row["physical_page"] for row in map_context}
    receipts = []
    for index, owned in enumerate(groups):
        remaining_work = ""
        while True:
            supplied = set(evidence["source_context_pages"])
            # Keep shared references and border pages alongside each owned group.
            # For recovery, all original evidence is re-read against retained work.
            if exhausted is not None or len(groups) == 1:
                context_pages = sorted(supplied)
            else:
                other_pages = {
                    p
                    for other in store.get("spine")["chapters"]
                    if other["id"] != chapter_id
                    for p in other["pages"]
                }
                core = set(chapter["pages"]) - other_pages or set(chapter["pages"])
                shared = supplied - core
                ordered = sorted(set(chapter["pages"]) & core)
                border = {
                    ordered[j]
                    for p in owned
                    if p in ordered
                    for j in (ordered.index(p) - 1, ordered.index(p) + 1)
                    if 0 <= j < len(ordered)
                }
                context_pages = sorted(set(owned) | shared | border)
            # Spatial claims can depend on a map far from this part's owned pages.
            # Keep its original pixels, not an earlier author's map summary.
            context_pages = sorted(set(context_pages) | map_pages)
            part_evidence = {
                **evidence,
                "source_evidence": evidence_pages(source, context_pages),
                "source_context_pages": context_pages,
                "image_pages": sorted(
                    set(context_pages) & set(store.get("visual-candidates") or [])
                ),
                "map_context": map_context,
                "owned_pages": owned,
                "part_index": index,
                "part_count": len(groups),
                "retained_chapter": retained,
                "unfinished_record_fields": pending,
                "unfinished_record_constraints": constraints,
                "remaining_work": remaining_work,
                "recovering_output": exhausted is not None,
            }
            images = workflow.source_images(context_pages)
            # Context overflow is partitioned as original evidence, never summaries.
            if not request_context("chapter-part", ChapterPart, part_evidence, images)["fits"]:
                raise NeedsAttention(
                    "Chapter part still exceeds context; exact page spans need repartitioning."
                )
            node = "chapter-part-" + chapter_id + "-" + digest(part_evidence)[:16]
            before = deepcopy(retained)
            try:
                part = (
                    await workflow.semantic.call(
                        node, "chapter-part", ChapterPart, part_evidence, images
                    )
                ).model_dump()
            except OutputExhausted as exc:
                addition, unfinished = closed_content(exc, chapter_id, wrapped=False)
                parents.append(exc.task_id)
                store.put(
                    "chapter-recovered-" + exc.task_id,
                    {
                        "parent_task_id": exc.task_id,
                        "response_id": exc.response_id,
                        "response_sha256": digest(exc.response),
                        "provisional_records": addition,
                        "unfinished_record_fields": unfinished,
                    },
                    digest(part_evidence),
                )
                root = next(
                    (v["value"] for v in completed_json_values(exc.output_text) if not v["path"]),
                    None,
                )
                if root is not None and root.get("complete") is True:
                    part = ChapterPart.model_validate(root).model_dump()
                    if part != root:
                        raise NeedsAttention(
                            "Closed exhausted part is not completely serialized."
                        ) from exc
                else:
                    retained = merge(retained, addition)
                    pending.extend(unfinished)
                    constraints.extend(unfinished_constraints(unfinished))
                    if retained == before:
                        raise NeedsAttention(
                            "Output recovery produced no new complete chapter records; paid work retained."
                        ) from exc
                    remaining_work = "Finish only missing records and unfinished source coverage."
                    continue
            if part["chapter_id"] != chapter_id:
                raise NeedsAttention("Chapter part returned a different chapter ID.")
            if part["complete"] and set(part["source_pages"]) != set(owned):
                raise NeedsAttention("Completed chapter part must account for all its owned pages.")
            if not set(part["source_pages"]) <= set(owned):
                raise NeedsAttention("Chapter part claims pages owned by a different part.")
            retained = merge(retained, part)
            receipt = {
                "node": node,
                "owned_pages": owned,
                "context_pages": context_pages,
                "complete": part["complete"],
                "input_context": request_context(
                    "chapter-part", ChapterPart, part_evidence, images
                ),
                "result_sha256": digest(part),
            }
            store.put(
                "chapter-part-receipt-" + digest(part_evidence), receipt, digest(part_evidence)
            )
            receipts.append(receipt)
            if part["complete"]:
                break
            if retained == before or not part["remaining_work"].strip():
                raise NeedsAttention("Chapter continuation made no progress; paid work retained.")
            remaining_work = part["remaining_work"]
    # source_pages is an ownership inventory, not inferred coverage from citations.
    retained["source_pages"] = chapter["pages"]
    bundle = Bundle.model_validate(retained).model_dump()
    validate_constraints(bundle, constraints)
    return bundle, {
        "mode": "output-recovery" if exhausted else "source-page-parts",
        "parent_task_ids": parents,
        "parts": receipts,
    }


async def author_chapter(workflow, evidence, images):
    chapter = evidence["requested_chapter"]
    groups = page_groups(workflow.store.get("spine"), chapter, workflow.store.get("source"))
    # Retained original requests keep the exact prompt/schema/evidence identity. This
    # check decides routing only; SemanticClient still verifies the full fingerprint.
    retained_task = has_original_task(workflow.store, chapter["id"])
    if not retained_task and (
        len(groups) > 1 or not request_context("chapters", Bundles, evidence, images)["fits"]
    ):
        return await author_parts(workflow, evidence, groups=groups)
    try:
        value = await workflow.semantic.call(
            "chapter-" + chapter["id"],
            "chapters",
            Bundles,
            evidence,
            images,
            **({"reuse_saved_request": True} if retained_task else {}),
        )
    except OutputExhausted as exc:
        return await author_parts(workflow, evidence, exhausted=exc)
    if len(value.chapters) != 1 or value.chapters[0].chapter_id != chapter["id"]:
        raise NeedsAttention("Authoring response must cover its one requested chapter.")
    return value.chapters[0].model_dump(), {"mode": "whole-chapter"}
