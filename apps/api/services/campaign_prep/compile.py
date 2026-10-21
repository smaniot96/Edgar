"""Deterministic semantic joins and playable assembly using independent engine records."""

from __future__ import annotations

import re

import tiktoken

from edgar_core.artifacts import text_digest
from edgar_core.campaign_semantics import Bundles, Spine
from edgar_core.manifests import AdventureManifest, manifest_digest
from ingestion.extract import ExtractedPage, Extraction
from ingestion.quality import check_text

from .store import NeedsAttention, digest


def derived(identity, suffix):
    value = identity + "_" + suffix
    if len(value) > 64:
        value = identity[: 54 - len(suffix)] + "_" + digest(identity)[:8] + "_" + suffix
    return value


class SemanticExtraction(Extraction):
    def markdown(self):
        return "\n\n".join(
            f"<!-- Authored campaign record {p.page}; not a PDF page -->\n\n{p.text}"
            for p in self.pages
        )


def split_embedding_spans(pages, passages, scenes, goals):
    """Respect the embedding model's per-input limit using exact character spans.

    Splits are local representation work; they never trigger semantic regeneration.
    """
    encoder = tiktoken.get_encoding("cl100k_base")
    text_by_page = {p.page: p.text for p in pages}
    expanded, remapped = [], {}
    for passage in passages:
        text = text_by_page[passage["page"]]
        start, stop = passage["start"], passage["end"]
        chunks = []
        while start < stop:
            content = text[start:stop]
            tokens = encoder.encode(content, disallowed_special=())
            end = stop
            if len(tokens) > 7500:
                _, offsets = encoder.decode_with_offsets(tokens)
                boundary = offsets[7000]
                breaks = list(re.finditer(r"\s+", content[:boundary]))
                end = start + (breaks[-1].end() if breaks else boundary)
            value = {
                **passage,
                "start": start,
                "end": end,
                "text_sha256": text_digest(text[start:end]),
            }
            chunks.append(value)
            start = end
        if len(chunks) > 1:
            for i, value in enumerate(chunks):
                value["passage_id"] = derived(passage["passage_id"], "part_" + str(i))
        remapped[passage["passage_id"]] = [v["passage_id"] for v in chunks]
        expanded.extend(chunks)
    for scene in scenes:
        scene["references"] = [new for old in scene["references"] for new in remapped[old]]
    for goal in goals:
        goal["passage_ids"] = [new for old in goal["passage_ids"] for new in remapped[old]]
    return expanded


def records(spine: dict, bundles: dict):
    result = {}

    def add(kind, row):
        key = row["id"]
        if key in result and result[key] != {"kind": kind, "value": row}:
            raise NeedsAttention("Conflicting shared identity: " + key)
        result[key] = {"kind": kind, "value": row}

    for entity in spine["entities"]:
        add("entity", entity)
    for secret in spine["secrets"]:
        add("secret", secret)
    for bundle in bundles["chapters"]:
        for kind, field in (
            ("entity", "new_entities"),
            ("secret", "new_secrets"),
            ("goal", "goals"),
            ("event", "events"),
        ):
            for row in bundle[field]:
                add(kind, row)
    return result


def dependencies(registry):
    """Consumer -> actual referenced records; no blanket chapter invalidation."""
    deps = {key: set() for key in registry}
    for key, record in registry.items():
        value, kind = record["value"], record["kind"]
        if kind == "event":
            refs = [
                value["location_id"],
                *value["participants"],
                *value["goal_ids"],
                *value["secret_ids"],
                *value["clue_ids"],
                *value.get("visible_entity_ids", []),
                *(r["entity_id"] for r in value.get("reveals", [])),
            ]
        elif kind == "goal":
            refs = [*value["event_ids"], *value["depends_on"]]
        elif kind == "secret":
            refs = value["known_by"]
        else:
            refs = []
        deps[key].update(refs)
    # Knowledge holders and setup/reveal events consume the truth, not vice versa.
    for key, rec in registry.items():
        if rec["kind"] == "secret":
            for holder in rec["value"]["known_by"]:
                if holder in deps:
                    deps[holder].add(key)
    return deps


def closure(registry, changed):
    deps = dependencies(registry)
    result = set(changed)
    while True:
        expanded = result | {key for key, refs in deps.items() if refs & result}
        if expanded == result:
            return sorted(result)
        result = expanded


def validate_semantics(spine, bundles, source=None):
    Spine.model_validate(spine)
    Bundles.model_validate(bundles)
    chapters = {c["id"] for c in spine["chapters"]}
    if (
        len(chapters) != len(spine["chapters"])
        or {b["chapter_id"] for b in bundles["chapters"]} != chapters
        or len(bundles["chapters"]) != len(chapters)
    ):
        raise NeedsAttention("Chapter bundle inventory does not match the spine.")
    registry = records(spine, bundles)
    if {"campaign_spine", "campaign_registry"} & (registry.keys() | chapters):
        raise NeedsAttention("Campaign uses a reserved engine identity.")
    for chapter in spine["chapters"]:
        if not set(chapter["entity_ids"] + chapter["secret_ids"]) <= registry.keys():
            raise NeedsAttention("Unknown chapter registry references: " + chapter["id"])
        if not set(chapter["depends_on"]) <= chapters:
            raise NeedsAttention("Unknown chapter dependencies: " + chapter["id"])
    for key, refs in dependencies(registry).items():
        if not refs <= registry.keys():
            raise NeedsAttention(
                "Unknown record references in " + key + ": " + str(sorted(refs - registry.keys()))
            )
    for record in registry.values():
        row = record["value"]
        if record["kind"] == "event":
            if not set(row["next_event_ids"]) <= {
                k for k, r in registry.items() if r["kind"] == "event"
            }:
                raise NeedsAttention("Unknown next event identity: " + row["id"])
            if not set(row.get("visible_entity_ids", [])) <= set(
                row["participants"] + row["clue_ids"]
            ):
                raise NeedsAttention("Visible entity must participate in the event: " + row["id"])
            if any(r["entity_id"] not in row["clue_ids"] for r in row.get("reveals", [])):
                raise NeedsAttention("Discovery reveal must reference an event clue: " + row["id"])
            if (
                registry[row["location_id"]]["kind"] != "entity"
                or registry[row["location_id"]]["value"]["kind"] != "place"
            ):
                raise NeedsAttention("Event location is not a place: " + row["id"])
            for e in row["encounters"]:
                if (
                    e["actor_id"] not in row["participants"]
                    or registry[e["actor_id"]]["value"].get("kind") != "npc"
                ):
                    raise NeedsAttention(
                        "Encounter actor must be an event participant: " + row["id"]
                    )
        if (
            record["kind"] == "secret"
            and not set(row["setup_chapters"] + row["reveal_chapters"]) <= chapters
        ):
            raise NeedsAttention("Secret chapter references are invalid.")
    if source is not None:
        pages = {p["page"]: {r["id"] for r in p["regions"]} for p in source.get("pages", [])}
        for chapter in spine["chapters"]:
            if not set(chapter["pages"]) <= pages.keys():
                raise NeedsAttention("Chapter cites nonexistent physical pages.")
        for key, rec in registry.items():
            origin = rec["value"]["origin"]
            cited = set(origin["pages"])
            if source["kind"] == "ai" and (cited or origin["regions"] or origin["kind"] == "pdf"):
                raise NeedsAttention(
                    "Authored material cannot cite fictitious PDF evidence: " + key
                )
            if not cited <= pages.keys():
                raise NeedsAttention("Origin cites nonexistent physical pages: " + key)
            regions = set().union(*(pages[p] for p in cited)) if cited else set()
            if not set(origin["regions"]) <= regions:
                raise NeedsAttention("Origin region is absent from its cited pages: " + key)
    return registry


def assemble(slug, edition, spine, bundles, mechanics):
    registry = validate_semantics(spine, bundles)
    by_chapter = {b["chapter_id"]: b for b in bundles["chapters"]}
    chapters = sorted(spine["chapters"], key=lambda c: (c["play_order"], c["id"]))
    ordered_events = [e for c in chapters for e in by_chapter[c["id"]]["events"]]
    scene_ids = {e["id"] for e in ordered_events}
    pages, passages, scenes, entities, goals = [], [], [], [], []
    catalogue = {r["name"].casefold(): r for r in mechanics}
    actor_bindings = {}
    grounded = []
    for event in ordered_events:
        for encounter in event["encounters"]:
            creature = catalogue.get(encounter["creature"].casefold())
            if not creature:
                raise NeedsAttention("Unverified encounter template: " + encounter["creature"])
            reference = {
                "kind": "template",
                "creature_name": creature["name"],
                "collection": creature["collection"],
                "source_sha256": creature["source_sha256"],
                "rules_edition": edition,
            }
            old = actor_bindings.get(encounter["actor_id"])
            if old and old != reference:
                raise NeedsAttention(
                    "Actor has conflicting mechanical bindings: " + encounter["actor_id"]
                )
            actor_bindings[encounter["actor_id"]] = reference
            grounded.append({"event_id": event["id"], **encounter, "verified": creature})
    for key, rec in registry.items():
        if rec["kind"] == "entity" and rec["value"]["kind"] in ("npc", "clue"):
            entities.append(
                {
                    "entity_id": key,
                    "kind": "actor" if rec["value"]["kind"] == "npc" else "clue",
                    "title": rec["value"]["name"],
                    "stat_reference": actor_bindings.get(key),
                }
            )
    for cindex, chapter in enumerate(chapters):
        bundle = by_chapter[chapter["id"]]
        for index, event in enumerate(bundle["events"]):
            public = event["public_description"].strip()
            private = "\n\n".join(
                [
                    "# " + event["title"],
                    "Trigger: " + event["trigger"],
                    "Timing: " + event["timing"],
                    event["dm_notes"],
                    "Provenance: " + str(event["origin"]),
                    "Encounters (original quantities, fit only during play): "
                    + str(
                        [
                            {k: g[k] for k in ("actor_id", "creature", "quantity", "adaptation")}
                            for g in grounded
                            if g["event_id"] == event["id"]
                        ]
                    ),
                    "Discovery opportunities: " + str(event.get("reveals", [])),
                ]
            )
            text = public + "\n\n" + private
            page = len(pages) + 1
            pages.append(ExtractedPage(page, text, "semantic-assembly-v2", check_text(text)))
            actor_ids = [
                key
                for key in event["participants"] + event["clue_ids"]
                if any(e["entity_id"] == key for e in entities)
            ]
            passages += [
                {
                    "passage_id": derived(event["id"], "public"),
                    "page": page,
                    "start": 0,
                    "end": len(public),
                    "text_sha256": text_digest(public),
                    "scene_ids": [event["id"]],
                    "entity_ids": event.get("visible_entity_ids", []),
                    "audience": "player",
                },
                {
                    "passage_id": derived(event["id"], "dm"),
                    "page": page,
                    "start": len(public) + 2,
                    "end": len(text),
                    "text_sha256": text_digest(private),
                    "scene_ids": [event["id"]],
                    "entity_ids": sorted(set(actor_ids)),
                    "audience": "planner",
                },
            ]
            reveal_ids = []
            for reveal_index, reveal in enumerate(event.get("reveals", [])):
                entity = next(e for e in entities if e["entity_id"] == reveal["entity_id"])
                flag = derived(reveal["entity_id"], "discovered")
                entity["discovery_flag"] = flag
                reveal_page = len(pages) + 1
                reveal_text = reveal["public_text"].strip()
                pages.append(
                    ExtractedPage(
                        reveal_page, reveal_text, "semantic-assembly-v2", check_text(reveal_text)
                    )
                )
                reveal_id = derived(event["id"], "reveal_" + str(reveal_index))
                reveal_ids.append(reveal_id)
                passages.append(
                    {
                        "passage_id": reveal_id,
                        "page": reveal_page,
                        "start": 0,
                        "end": len(reveal_text),
                        "text_sha256": text_digest(reveal_text),
                        "scene_ids": [event["id"]],
                        "entity_ids": [reveal["entity_id"]],
                        "audience": "player",
                        "requires": {flag: "discovered"},
                    }
                )
            links = list(event["next_event_ids"])
            if index == len(bundle["events"]) - 1 and cindex + 1 < len(chapters):
                links.append(by_chapter[chapters[cindex + 1]["id"]]["events"][0]["id"])
            scenes.append(
                {
                    "scene_id": event["id"],
                    "title": event["title"],
                    "links": [
                        {"scene_id": key}
                        for key in dict.fromkeys(links)
                        if key in scene_ids and key != event["id"]
                    ],
                    "references": [
                        derived(event["id"], "public"),
                        derived(event["id"], "dm"),
                        "campaign_registry",
                        *reveal_ids,
                    ],
                }
            )
        for goal in bundle["goals"]:
            goals.append(
                {
                    "canonical_id": goal["id"],
                    "title": goal["title"],
                    "scene_ids": goal["event_ids"],
                    "passage_ids": ["campaign_registry"],
                    "completion_condition": goal["completion"],
                }
            )
    # Private registry is stored once; every scene can retrieve it as planner context.
    import json

    guide = "\n\n".join(
        [
            "# " + spine["title"],
            spine["premise"],
            "Setting: " + spine["setting"],
            "Opening hook: " + spine["hook"],
            "Possible ending: " + spine["ending"],
            spine["dm_guidance"],
            "# Shared identities, knowledge, goals and future reveals\n"
            + json.dumps(registry, ensure_ascii=False, indent=2),
            "# Chapter guidance\n"
            + "\n\n".join(c["title"] + "\n" + by_chapter[c["id"]]["dm_guidance"] for c in chapters),
            "The live DM recognizes credible creative solutions. Events are opportunities, not committed outcomes. Preserve hidden truths until earned. The later selected hero is always the protagonist. Combat and state are engine-owned.",
        ]
    )
    page = len(pages) + 1
    pages.append(ExtractedPage(page, guide, "semantic-assembly-v2", check_text(guide)))
    passages.append(
        {
            "passage_id": "campaign_registry",
            "page": page,
            "start": 0,
            "end": len(guide),
            "text_sha256": text_digest(guide),
            "scene_ids": sorted(scene_ids),
            "audience": "planner",
        }
    )
    extraction = SemanticExtraction(slug, "", len(pages), pages, rules_edition=edition)
    extraction.source_sha256 = extraction.text_sha256()
    passages = split_embedding_spans(pages, passages, scenes, goals)
    manifest = AdventureManifest(
        source=slug,
        source_sha256=extraction.source_sha256,
        extraction_sha256=extraction.text_sha256(),
        rules_edition=edition,
        opening_scene_id=ordered_events[0]["id"],
        scenes=scenes,
        entities=entities,
        passages=passages,
        objectives=goals,
    )
    # One goal representation (objectives) at the engine boundary; no duplicate milestones.
    return {
        "manifest": manifest.model_dump(mode="json"),
        "guide": guide,
        "pages": [{"page": p.page, "text": p.text} for p in pages],
        "registry": registry,
        "grounded": grounded,
        "semantic_sha256": digest({"spine": spine, "bundles": bundles}),
        "manifest_sha256": manifest_digest(manifest),
    }


def make_extraction(compiled):
    value = compiled["manifest"]
    return SemanticExtraction(
        value["source"],
        value["source_sha256"],
        len(compiled["pages"]),
        [
            ExtractedPage(p["page"], p["text"], "semantic-assembly-v2", check_text(p["text"]))
            for p in compiled["pages"]
        ],
        rules_edition=value["rules_edition"],
    )
