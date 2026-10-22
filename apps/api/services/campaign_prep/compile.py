"""Deterministic semantic joins and playable assembly using independent engine records."""

from __future__ import annotations

import json
import re

import tiktoken

from edgar_core.artifacts import text_digest
from edgar_core.campaign_semantics import Bundles, Spine
from edgar_core.manifests import AdventureManifest, manifest_digest
from ingestion.extract import ExtractedPage
from ingestion.quality import check_text
from ingestion.semantic import SemanticRecordExtraction as SemanticExtraction, SemanticRecordPage

from .mechanics import source_check
from .store import NeedsAttention, digest


def derived(identity, suffix):
    value = identity + "_" + suffix
    if len(value) > 64:
        value = identity[: 54 - len(suffix)] + "_" + digest(identity)[:8] + "_" + suffix
    return value


def chapter_source_pages(spine, chapter):
    """Original chapter pages plus the evidence behind its shared identities/truths.

    The planner's prose can recognize an appendix without allocating its page. Follow
    explicit source references locally so the next author/DM does not lose that detail.
    Base chapter membership stays stable; this is its supporting source context.
    """
    wanted = set(chapter["entity_ids"] + chapter["secret_ids"])
    return sorted(
        set(chapter["pages"])
        | {
            page
            for row in spine["entities"] + spine["secrets"]
            if row["id"] in wanted
            for page in row["origin"]["pages"]
        }
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
    for profile in bundles.get("mechanical_profiles", []):
        add(
            "mechanical_profile",
            {
                **profile,
                "origin": {
                    "kind": "pdf" if profile.get("source_pages") else "authored",
                    "pages": profile.get("source_pages", []),
                    "regions": [],
                    "note": "Shared mechanical definition",
                    "uncertain": False,
                },
            },
        )
    for bundle in bundles["chapters"]:
        if any(key in bundle for key in ("source_pages", "source_notes", "items", "checks")):
            key = derived(bundle["chapter_id"], "content")
            add(
                "chapter",
                {
                    "id": key,
                    "chapter_id": bundle["chapter_id"],
                    "source_pages": bundle.get("source_pages", []),
                    "source_notes": bundle.get("source_notes", ""),
                    "items": bundle.get("items", []),
                    "checks": bundle.get("checks", []),
                    "event_ids": [event["id"] for event in bundle["events"]],
                    "origin": {
                        "kind": "pdf" if bundle.get("source_pages") else "authored",
                        "pages": bundle.get("source_pages", []),
                        "regions": [],
                        "note": "Chapter source material and procedures",
                        "uncertain": False,
                    },
                },
            )
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
                *(r["profile_id"] for r in value["encounters"] if r.get("profile_id")),
            ]
        elif kind == "goal":
            refs = [*value["event_ids"], *value["depends_on"]]
        elif kind == "secret":
            refs = value["known_by"]
        elif kind == "chapter":
            # Event scopes reference stable IDs; they do not consume event prose.
            # Making both edges would turn every local edit into a chapter rewrite.
            refs = [item["entity_id"] for item in value["items"]]
        else:
            refs = []
        deps[key].update(refs)
    # Knowledge holders and setup/reveal events consume the truth, not vice versa.
    for key, rec in registry.items():
        if rec["kind"] == "secret":
            for holder in rec["value"]["known_by"]:
                if holder in deps:
                    deps[holder].add(key)
        elif rec["kind"] == "chapter":
            for event_id in rec["value"]["event_ids"]:
                if event_id in deps:
                    deps[event_id].add(key)
        elif rec["kind"] == "event":
            for encounter in rec["value"]["encounters"]:
                if encounter.get("profile_id") and encounter["actor_id"] in deps:
                    deps[encounter["actor_id"]].add(encounter["profile_id"])
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
    for bundle in bundles["chapters"]:
        if "source_pages" in bundle and set(bundle["source_pages"]) != set(
            next(c["pages"] for c in spine["chapters"] if c["id"] == bundle["chapter_id"])
        ):
            raise NeedsAttention(
                "Chapter author did not acknowledge all assigned source pages: "
                + bundle["chapter_id"]
            )
        event_ids = {e["id"] for e in bundle["events"]}
        for item in bundle.get("items", []):
            entity = registry.get(item["entity_id"])
            if not entity or entity["kind"] != "entity" or entity["value"]["kind"] != "clue":
                raise NeedsAttention(
                    "Source item requires a canonical clue entity: " + item["entity_id"]
                )
            if not item["event_ids"] or not set(item["event_ids"]) <= event_ids:
                raise NeedsAttention("Source item references events outside its chapter.")
        for check in bundle.get("checks", []):
            if not check["event_ids"] or not set(check["event_ids"]) <= event_ids:
                raise NeedsAttention("Source check references events outside its chapter.")
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
            visible = set(row.get("visible_entity_ids", []))
            places = {
                key
                for key in visible
                if registry[key]["kind"] == "entity" and registry[key]["value"]["kind"] == "place"
            }
            if not visible <= set(row["participants"] + row["clue_ids"]) | places:
                raise NeedsAttention("Visible entity must participate in the event: " + row["id"])
            if any(
                r["entity_id"] not in row["clue_ids"] + row["participants"]
                for r in row.get("reveals", [])
            ):
                raise NeedsAttention(
                    "Discovery reveal must reference an event clue or participant: " + row["id"]
                )
            if (
                registry[row["location_id"]]["kind"] != "entity"
                or registry[row["location_id"]]["value"]["kind"] != "place"
            ):
                raise NeedsAttention("Event location is not a place: " + row["id"])
            for e in row["encounters"]:
                if e.get("profile_id") and (
                    e["profile_id"] not in registry
                    or registry[e["profile_id"]]["kind"] != "mechanical_profile"
                ):
                    raise NeedsAttention("Unknown mechanical profile reference: " + e["profile_id"])
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
            if (
                rec["kind"] == "mechanical_profile"
                and source["kind"] != "pdf"
                and rec["value"].get("source_stat_block")
            ):
                raise NeedsAttention("Authored encounter must use verified catalogue mechanics.")
        for bundle in bundles["chapters"]:
            chapter = next(c for c in spine["chapters"] if c["id"] == bundle["chapter_id"])
            context_pages = set(chapter_source_pages(spine, chapter))
            for row in bundle.get("items", []) + bundle.get("checks", []):
                origin = row["origin"]
                cited = set(origin["pages"])
                if not cited <= context_pages:
                    raise NeedsAttention(
                        "Chapter procedure cites evidence outside its source context."
                    )
                if source["kind"] == "ai" and (
                    cited or origin["regions"] or origin["kind"] == "pdf"
                ):
                    raise NeedsAttention("Authored procedure cannot cite fictitious PDF evidence.")
                regions = set().union(*(pages[p] for p in cited)) if cited else set()
                if not set(origin["regions"]) <= regions:
                    raise NeedsAttention("Procedure origin region is absent from its cited pages.")
            for event in bundle["events"]:
                for encounter in event["encounters"]:
                    if encounter.get("source_stat_block") and source["kind"] != "pdf":
                        raise NeedsAttention(
                            "Authored encounter must use verified catalogue mechanics."
                        )
                    if not set(encounter.get("source_pages", [])) <= context_pages:
                        raise NeedsAttention(
                            "Source profile cites evidence outside its source context."
                        )
    return registry


def retain_chapter_source(
    source, spine, chapters, by_chapter, pages, passages, scenes, goals, visuals
):
    """Publish immutable OCR as private chapter evidence, independently of author prose.

    A shared appendix is stored once and linked into every chapter that needs it.
    It is never made player evidence and it does not unlock any discovery. Retained
    images/layout remain source references rather than invented narrative text.
    """
    if not source or source.get("kind") != "pdf":
        return []
    source_pages = {p["page"]: p for p in source.get("pages", [])}
    assigned = {number for chapter in chapters for number in chapter["pages"]}
    if set(source_pages) - assigned:
        raise NeedsAttention(
            "Physical source pages are absent from the chapter plan: "
            + str(sorted(set(source_pages) - assigned))
        )
    chapters_by_page = {}
    for chapter in chapters:
        for number in chapter_source_pages(spine, chapter):
            chapters_by_page.setdefault(number, []).append(chapter["id"])
    if set(source_pages) - chapters_by_page.keys():
        raise NeedsAttention(
            "Physical source pages are absent from the chapter plan: "
            + str(sorted(set(source_pages) - chapters_by_page.keys()))
        )
    receipts = []
    for number, chapter_ids in sorted(chapters_by_page.items()):
        original = source_pages[number]
        relevant_scenes = [
            event["id"] for chapter_id in chapter_ids for event in by_chapter[chapter_id]["events"]
        ]
        passage_id = f"source_page_{number:04d}"
        if any(p["passage_id"] == passage_id for p in passages):
            raise NeedsAttention("Reserved source passage identity: " + passage_id)
        metadata = {
            "physical_page": number,
            "source_sha256": source.get("sha256"),
            "image": original.get("image"),
            "image_sha256": original.get("image_sha256"),
            "source_directory": source.get("directory"),
            "width": original.get("width"),
            "height": original.get("height"),
            "quality": original.get("quality", {}),
            "regions": [
                {k: r[k] for k in ("id", "bbox", "reading_order", "relationships") if k in r}
                for r in original.get("regions", [])
            ],
            "visual_evidence": visuals.get(str(number), visuals.get(number)) if visuals else None,
        }
        text = (
            f"# Private original campaign source — physical page {number}\n\n"
            "This is retained OCR evidence, including future reveals and unearned facts. "
            "Use it to recover source detail and adjudication conditions; do not quote it "
            "to players or treat its contents as discovered. OCR table/column ownership "
            "can be uncertain; consult retained image evidence when available. Engine "
            "state and verified executable definitions remain authoritative for execution. "
            "For adventure facts the original source takes precedence over accidental "
            "contradictions in prepared notes; an explicitly documented adaptation states "
            "its original fact, replacement and reason and is the campaign's chosen version. "
            "Committed play, earned knowledge and resolved outcomes take precedence over "
            "both versions. Never reset state, undo a player choice or blend conflicting facts.\n\n"
            + json.dumps(metadata, ensure_ascii=False, sort_keys=True)
            + "\n\n## Original OCR (unabridged)\n\n"
            + original["text"]
        )
        page = len(pages) + 1
        pages.append(ExtractedPage(page, text, "retained-source-ocr-v2", check_text(text)))
        passages.append(
            {
                "passage_id": passage_id,
                "page": page,
                "start": 0,
                "end": len(text),
                "text_sha256": text_digest(text),
                "scene_ids": relevant_scenes,
                "audience": "planner",
            }
        )
        for scene in scenes:
            if scene["scene_id"] in relevant_scenes:
                scene["references"].append(passage_id)
        for goal in goals:
            if set(goal["scene_ids"]) & set(relevant_scenes):
                goal["passage_ids"].append(passage_id)
        receipts.append(
            {
                "physical_page": number,
                "passage_id": passage_id,
                "chapter_ids": chapter_ids,
                "scene_ids": relevant_scenes,
                "ocr_text_sha256": text_digest(original["text"]),
                "image_sha256": original.get("image_sha256"),
            }
        )
    return receipts


def assemble(slug, edition, spine, bundles, mechanics, source=None, visuals=None):
    from .profiles import canonical_profiles, resolved_encounter

    bundles = canonical_profiles(bundles, mechanics)
    registry = validate_semantics(spine, bundles, source)
    by_chapter = {b["chapter_id"]: b for b in bundles["chapters"]}
    chapters = sorted(spine["chapters"], key=lambda c: (c["play_order"], c["id"]))
    ordered_events = [e for c in chapters for e in by_chapter[c["id"]]["events"]]
    scene_ids = {e["id"] for e in ordered_events}
    pages, passages, scenes, entities, goals, objects, policies = [], [], [], [], [], [], []
    catalogue = {r["name"].casefold(): r for r in mechanics}
    actor_bindings = {}
    grounded = []
    source_profiles = {}
    definitions = {row["id"]: row for row in bundles.get("mechanical_profiles", [])}
    parsed_profiles = {}
    capability_findings = []
    for event in ordered_events:
        for appearance in event["encounters"]:
            encounter = resolved_encounter(appearance, definitions)
            profile_id = encounter["profile_id"]
            source_stat_block = encounter.get("source_stat_block", "")
            if source_stat_block.strip():
                from .mechanics import source_profile

                try:
                    profile = parsed_profiles.get(profile_id)
                    if profile is None:
                        profile = source_profile(
                            source_stat_block,
                            name=encounter["creature"],
                            source_evidence={
                                "pages": encounter.get("source_pages", []),
                                "source_sha256": source.get("sha256") if source else None,
                            },
                        )
                        parsed_profiles[profile_id] = profile
                except ValueError as exc:
                    raise NeedsAttention(
                        "Invalid source profile for " + encounter["actor_id"] + ": " + str(exc)
                    ) from exc
                creature = profile
                passage_id = derived(profile_id, "statistics")
                reference = {
                    "kind": "module",
                    "creature_name": encounter["creature"],
                    "passage_id": passage_id,
                }
                old_profile = source_profiles.get(encounter["actor_id"])
                if (
                    old_profile
                    and re.sub(r"\s+", " ", old_profile["text"]).strip().casefold()
                    != re.sub(r"\s+", " ", profile["text"]).strip().casefold()
                ):
                    raise NeedsAttention(
                        "Actor has conflicting source profiles: " + encounter["actor_id"]
                    )
                source_profiles.setdefault(encounter["actor_id"], profile)
                capability_findings.extend(
                    {"actor_id": encounter["actor_id"], "event_id": event["id"], "detail": detail}
                    for detail in profile.get("capability_findings", [])
                )
            else:
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
            grounded.append(
                {"event_id": event["id"], **appearance, "creature": encounter["creature"]}
            )
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
            # Knowledge has its own identity. Encountering an NPC must never disclose
            # their private history, nor may an early clue unlock a later revelation.
            discoveries = []
            for reveal_index, reveal in enumerate(event.get("reveals", [])):
                target_id = derived(event["id"], "knowledge_" + str(reveal_index))
                if target_id in registry:
                    raise NeedsAttention("Reserved discovery identity: " + target_id)
                entities.append(
                    {
                        "entity_id": target_id,
                        "kind": "clue",
                        # The referenced identity may itself name a future answer.
                        # Only public_text establishes what this discovery discloses.
                        "title": "Discovery " + str(reveal_index + 1),
                        "discovery_flag": derived(target_id, "discovered"),
                    }
                )
                discoveries.append({**reveal, "target_id": target_id})
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
                    "Discovery opportunities: " + str(discoveries),
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
            public_entities = [
                key
                for key in event.get("visible_entity_ids", [])
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
                    "entity_ids": public_entities,
                    "known_scene_ids": [event["id"]]
                    if event["location_id"] in event.get("visible_entity_ids", [])
                    else [],
                    "audience": "player",
                },
                {
                    "passage_id": derived(event["id"], "dm"),
                    "page": page,
                    "start": len(public) + 2,
                    "end": len(text),
                    "text_sha256": text_digest(private),
                    "scene_ids": [event["id"]],
                    "entity_ids": sorted(set(actor_ids + [d["target_id"] for d in discoveries])),
                    "audience": "planner",
                },
            ]
            reveal_ids = []
            for reveal_index, reveal in enumerate(discoveries):
                flag = derived(reveal["target_id"], "discovered")
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
                        "entity_ids": [reveal["target_id"]],
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
    for profile_id, profile in parsed_profiles.items():
        text = profile["text"]
        page = len(pages) + 1
        passage_id = derived(profile_id, "statistics")
        related_actors = sorted({g["actor_id"] for g in grounded if g["profile_id"] == profile_id})
        related_scenes = [
            e["id"]
            for e in ordered_events
            if any(encounter["profile_id"] == profile_id for encounter in e["encounters"])
        ]
        pages.append(ExtractedPage(page, text, "verified-source-statistics-v2", check_text(text)))
        passages.append(
            {
                "passage_id": passage_id,
                "page": page,
                "start": 0,
                "end": len(text),
                "text_sha256": text_digest(text),
                "scene_ids": related_scenes,
                "entity_ids": related_actors,
                "audience": "planner",
            }
        )
        for scene in scenes:
            if scene["scene_id"] in related_scenes:
                scene["references"].append(passage_id)

    def private_procedure(passage_id, title, body, event_ids, entity_ids=None):
        if any(p["passage_id"] == passage_id for p in passages):
            raise NeedsAttention("Conflicting private procedure identity: " + passage_id)
        text = "# " + title + "\n\n" + body
        page = len(pages) + 1
        pages.append(ExtractedPage(page, text, "source-procedure-v2", check_text(text)))
        passages.append(
            {
                "passage_id": passage_id,
                "page": page,
                "start": 0,
                "end": len(text),
                "text_sha256": text_digest(text),
                "scene_ids": event_ids,
                "entity_ids": entity_ids or [],
                "audience": "planner",
            }
        )
        for scene in scenes:
            if scene["scene_id"] in event_ids:
                scene["references"].append(passage_id)

    item_objects = {}
    for chapter in chapters:
        bundle = by_chapter[chapter["id"]]
        if any(key in bundle for key in ("source_pages", "source_notes", "items", "checks")):
            private_procedure(
                derived(chapter["id"], "content"),
                chapter["title"] + " — detailed DM reference",
                bundle["dm_guidance"]
                + "\n\n"
                + bundle.get("source_notes", "")
                + "\n\nAssigned physical source pages: "
                + str(chapter["pages"]),
                [event["id"] for event in bundle["events"]],
            )
        for item in bundle.get("items", []):
            entity_id = item["entity_id"]
            object_id = derived(entity_id, "object")
            passage_id = derived(chapter["id"], "item_" + digest(entity_id)[:12])
            private_procedure(
                passage_id,
                registry[entity_id]["value"]["name"] + " — original item procedure",
                item["procedure"]
                + "\n\nProvenance: "
                + json.dumps(item["origin"], ensure_ascii=False)
                + "\n\nEngine support: canonical item custody (acquire, transfer, consume). "
                "This procedure does not define an executable use operation. Charges, timed "
                "recharge, attunement, transformation and damage require separately supported "
                "engine operations; retained prose must not be presented as committed execution.",
                item["event_ids"],
                [entity_id],
            )
            obj = item_objects.setdefault(
                entity_id,
                {
                    "canonical_id": object_id,
                    "title": registry[entity_id]["value"]["name"],
                    "scene_ids": [],
                    "passage_ids": [],
                    "statuses": ["acquired", "consumed", "moved"],
                    "operations": [],
                    "portable": item["portable"],
                    "consumable": item["consumable"],
                },
            )
            if (obj["portable"], obj["consumable"]) != (item["portable"], item["consumable"]):
                raise NeedsAttention("Conflicting source item properties: " + entity_id)
            obj["scene_ids"] = list(dict.fromkeys(obj["scene_ids"] + item["event_ids"]))
            obj["passage_ids"].append(passage_id)
            for entity in entities:
                if entity["entity_id"] == entity_id:
                    entity["object_id"] = object_id
            capability_findings.append(
                {
                    "entity_id": entity_id,
                    "chapter_id": chapter["id"],
                    "detail": "Source item procedure retained; engine supports canonical custody "
                    "but has no executable use operation for this authored item.",
                    "source_pages": item["origin"]["pages"],
                }
            )
        for check in bundle.get("checks", []):
            engine_check = source_check(check["check"])
            passage_id = derived(check["id"], "procedure")
            private_procedure(
                passage_id,
                check["action"],
                f"Engine check: {engine_check}; DC {check['dc']}.\n\n"
                + "Source check identifier: "
                + check["check"]
                + "\n\n"
                + "Source success guidance: "
                + check["success_text"]
                + "\n\nSource failure guidance: "
                + check["failure_text"]
                + "\n\nProvenance: "
                + json.dumps(check["origin"], ensure_ascii=False)
                + "\n\nEngine support: fixed-DC roll only. Source success/failure guidance "
                "is private reference; this policy has no authored outcome effects. A successful "
                "roll does not itself commit a discovery or make an associated clue known. "
                "It does not authorize inventory, damage, transformation or other persistent effects. "
                "This is an available approach, not a compulsory solution or route.",
                check["event_ids"],
            )
            policies.append(
                {
                    "policy_id": check["id"],
                    "action": check["action"],
                    "scene_ids": check["event_ids"],
                    "passage_ids": [passage_id],
                    "mode": "check",
                    "check": engine_check,
                    "dc": check["dc"],
                    "on_success": [],
                    "on_failure": [],
                }
            )
            capability_findings.append(
                {
                    "check_id": check["id"],
                    "chapter_id": chapter["id"],
                    "detail": "Fixed-DC source check supports the roll only. Its private "
                    "success/failure guidance is retained, but no associated discovery or "
                    "other persistent outcome is committed by this policy.",
                    "source_pages": check["origin"]["pages"],
                }
            )
    objects.extend(item_objects.values())

    source_receipts = retain_chapter_source(
        source,
        spine,
        chapters,
        by_chapter,
        pages,
        passages,
        scenes,
        goals,
        visuals,
    )
    # Private registry is stored once; every scene can retrieve it as planner context.

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
            + "\n\n".join(
                c["title"]
                + "\n"
                + by_chapter[c["id"]]["dm_guidance"]
                + "\n"
                + by_chapter[c["id"]].get("source_notes", "")
                for c in chapters
            ),
            "# Declared engine capability limits\n"
            + json.dumps(capability_findings, ensure_ascii=False)
            + "\nRetained source text is not proof that every trait is engine-executable. "
            "Do not invent rolls, state changes or substitute creature statistics to conceal a limitation.",
            "# Original source reference index\n"
            + json.dumps(source_receipts, ensure_ascii=False)
            + "\nChapter source passages retain the original OCR, page image references, layout and "
            "quality diagnostics. They are private DM evidence, not discovered player knowledge. "
            "Consult them for omitted detail; source procedures do not authorize unsupported engine effects.",
            "The live DM recognizes credible creative solutions. Events are opportunities, not committed outcomes. Preserve hidden truths until earned. The later selected hero is always the protagonist. Combat and state are engine-owned.",
        ]
    )
    # The downloadable guide can contain the complete campaign. Ordinary play only
    # receives a compact global dossier plus its own chapter evidence; copying all
    # event notes and stat blocks into every scene wastes context and conceals detail.
    shared_guide = "\n\n".join(
        [
            "# " + spine["title"],
            spine["premise"],
            "Setting: " + spine["setting"],
            "Opening hook: " + spine["hook"],
            "Possible ending: " + spine["ending"],
            spine["dm_guidance"],
            "# Shared identities, knowledge, goals and future reveals\n"
            + json.dumps(
                {
                    key: row
                    for key, row in registry.items()
                    if row["kind"] not in {"event", "chapter"}
                },
                ensure_ascii=False,
            ),
            "# Chapter index\n"
            + json.dumps(
                [
                    {
                        "id": chapter["id"],
                        "title": chapter["title"],
                        "brief": chapter["brief"],
                        "play_order": chapter["play_order"],
                        "source_pages": chapter["pages"],
                    }
                    for chapter in chapters
                ],
                ensure_ascii=False,
            ),
            "Current chapter details, exact source profiles, item procedures and unabridged OCR "
            "are retrieved privately with the current scene. They contain hidden and future facts. "
            "Allow credible creative solutions; the later selected hero is always the protagonist. "
            "Only supported engine operations may commit numerical mechanics and state.",
        ]
    )
    page = len(pages) + 1
    pages.append(
        ExtractedPage(page, shared_guide, "semantic-assembly-v2", check_text(shared_guide))
    )
    passages.append(
        {
            "passage_id": "campaign_registry",
            "page": page,
            "start": 0,
            "end": len(shared_guide),
            "text_sha256": text_digest(shared_guide),
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
        objects=objects,
        resolution_policies=policies,
    )
    # One goal representation (objectives) at the engine boundary; no duplicate milestones.
    return {
        "manifest": manifest.model_dump(mode="json"),
        "guide": guide,
        "pages": [{"page": p.page, "text": p.text} for p in pages],
        "registry": registry,
        "grounded": grounded,
        "source_references": source_receipts,
        "source_profiles": parsed_profiles,
        "mechanical_profiles": {
            profile_id: parsed_profiles.get(profile_id)
            or catalogue.get(profile["creature"].casefold())
            for profile_id, profile in definitions.items()
        },
        "capability_findings": capability_findings,
        "compatibility_policy": "required-capabilities-1",
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
            SemanticRecordPage(
                p["page"],
                p["text"],
                "semantic-assembly-v2",
                {"characters": len(p["text"]), "validation": "reviewed-semantic-manifest"},
            )
            for p in compiled["pages"]
        ],
        rules_edition=value["rules_edition"],
    )
