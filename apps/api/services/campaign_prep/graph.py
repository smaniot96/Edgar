"""The new source -> spine -> bundles -> continuity -> assembly -> review graph.

Only this StateGraph dispatches campaign semantic tasks. SQLite is the state authority;
the in-memory graph state is an address, never an independent checkpoint scheduler.
"""

from __future__ import annotations

import asyncio
import copy
import json
import re
import sys
import time
from pathlib import Path
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from edgar_core.campaign_semantics import (
    Bundles,
    ContinuityPatch,
    Patch,
    Review,
    SourceReading,
    Spine,
)

from .chapter_parts import author_chapter
from .compile import (
    assemble,
    chapter_source_pages,
    closure,
    dependencies,
    derived,
    records,
    validate_semantics,
)
from .field_edits import apply_field_edits
from .mechanical_findings import mechanical_findings
from .mechanics import catalogue
from .profiles import canonical_profiles
from .provider import SemanticClient, request_context
from .source import (
    VISUAL_CANDIDATE_VERSION,
    acquire_pdf,
    candidate_visual_pages,
    evidence_pages,
    packets,
    token_count,
    visual_page_reasons,
)
from .store import NeedsAttention, Store, atomic_json, digest
from .structural_repair import repair_structure


class Address(TypedDict):
    slug: str


def retain_repair_event_inventory(registry, bundles, findings=(), explicit_event_ids=()):
    """Scoped corrections retain omitted bindings; explicit actor moves remain moves."""
    events = {event["id"]: event for bundle in bundles["chapters"] for event in bundle["events"]}
    directly_affected = {key for finding in findings for key in finding["record_ids"]} | set(
        explicit_event_ids
    )
    additions = {}
    registered_participants = {}
    for key, event in events.items():
        original = registry[key]["value"]
        old_actors = {row["actor_id"] for row in original["encounters"]}
        actors = [row["actor_id"] for row in event["encounters"]]
        if len(actors) != len(set(actors)):
            raise NeedsAttention("Event repair repeated an encounter actor.")
        if key in explicit_event_ids:
            for actor in actors:
                identity = registry.get(actor)
                if (
                    actor not in event["participants"]
                    and identity
                    and identity["kind"] == "entity"
                    and identity["value"]["kind"] == "npc"
                ):
                    # An explicit encounter binding already places this known NPC
                    # in the scene. Repair only the redundant participation join;
                    # no new actor, mechanics or narrative population is inferred.
                    event["participants"].append(actor)
                    registered_participants.setdefault(key, []).append(actor)
        for actor in set(actors) - old_actors:
            if actor in event["participants"]:
                additions.setdefault(actor, []).append(key)
    normalizations = []
    for key, event in events.items():
        original = registry[key]["value"]
        guidance_mode = None
        retained_opportunities = []
        retained_public_description = (
            key not in directly_affected
            and event["public_description"] != original["public_description"]
        )
        if retained_public_description:
            event["public_description"] = original["public_description"]
        old_notes, new_notes = original["dm_notes"], event["dm_notes"]
        if old_notes and old_notes not in new_notes:
            correction = new_notes or "No replacement DM guidance supplied."
            event["dm_notes"] = (
                "Accepted correction supersedes any conflicting earlier guidance.\n\n"
                + correction
                + "\n\nRetained earlier event reference:\n\n"
                + old_notes
                + "\n\nEarlier entry opportunity (reference; correction takes precedence):\n"
                + original["trigger"]
                + "\n\nEarlier timing (reference; correction takes precedence):\n"
                + original["timing"]
            )
            guidance_mode = "paid_correction_layer"
        # A chapter-reference correction can add an opportunity, but its
        # incidental event restatement cannot silently remove an accepted route.
        # Direct event findings retain authority to replace trigger/timing.
        if key not in directly_affected:
            for field in ("trigger", "timing"):
                if original[field] and original[field] not in event[field]:
                    event[field] = (
                        original[field]
                        + "\n\nAdditional accepted "
                        + ("entry opportunity" if field == "trigger" else "timing")
                        + ":\n"
                        + event[field]
                    )
                    retained_opportunities.append(field)
        proposed = {row["actor_id"]: row for row in event["encounters"]}
        old_actors = {row["actor_id"] for row in original["encounters"]}
        retained, relocated, rebound, encounters = [], {}, [], []
        for row in original["encounters"]:
            actor = row["actor_id"]
            mechanics = {field: value for field, value in row.items() if field != "actor_id"}
            candidates = [
                candidate
                for identity, candidate in proposed.items()
                if identity not in old_actors
                and {field: value for field, value in candidate.items() if field != "actor_id"}
                == mechanics
            ]
            # An unchanged same-actor binding plus another identical group is an
            # addition. Otherwise an exact mechanical match under a new identity
            # repairs the actor reference, including formerly shared actor IDs.
            if actor in proposed and proposed[actor] == row:
                encounters.append(proposed.pop(actor))
            elif len(candidates) == 1:
                replacement = proposed.pop(candidates[0]["actor_id"])
                encounters.append(replacement)
                rebound.append(
                    {
                        "from_actor_id": actor,
                        "to_actor_id": replacement["actor_id"],
                        "creature": row.get("creature", row.get("profile_id")),
                        "quantity": row["quantity"],
                    }
                )
            elif actor in proposed:
                encounters.append(proposed.pop(actor))
            elif actor not in event["participants"] and additions.get(actor):
                # Removing both the binding and participation while explicitly
                # adding that actor elsewhere expresses a move, not an omission.
                relocated[actor] = additions[actor]
            else:
                encounters.append(copy.deepcopy(row))
                retained.append(actor)
                if actor not in event["participants"]:
                    event["participants"].append(actor)
        encounters.extend(proposed.values())
        event["encounters"] = encounters
        supplied_reveals = {row["entity_id"] for row in event["reveals"]}
        omitted_reveals = [
            row for row in original.get("reveals", []) if row["entity_id"] not in supplied_reveals
        ]
        supplied_groups = {}
        for row in event["reveals"]:
            supplied_groups.setdefault(row["entity_id"], []).append(row)
        ordered_reveals, replaced_groups = [], set()
        for row in original.get("reveals", []):
            entity = row["entity_id"]
            if entity not in supplied_groups:
                ordered_reveals.append(copy.deepcopy(row))
            elif entity not in replaced_groups:
                ordered_reveals.extend(supplied_groups[entity])
                replaced_groups.add(entity)
        for entity, rows in supplied_groups.items():
            if entity not in replaced_groups:
                ordered_reveals.extend(rows)
        event["reveals"] = ordered_reveals
        if omitted_reveals:
            # Existing public text/conditions supplied for an identity replace its
            # earlier reveal verbatim. Absent identities keep their earned reveal.
            for row in omitted_reveals:
                entity = row["entity_id"]
                if entity not in event["participants"] + event["clue_ids"]:
                    field = "clue_ids" if entity in original["clue_ids"] else "participants"
                    event[field].append(entity)
        if (
            retained
            or relocated
            or rebound
            or omitted_reveals
            or guidance_mode
            or retained_opportunities
            or retained_public_description
            or registered_participants.get(key)
        ):
            normalizations.append(
                {
                    "event_id": key,
                    "retained_encounter_actor_ids": retained,
                    "relocated_encounter_actor_ids": relocated,
                    "rebound_encounter_actors": rebound,
                    "retained_reveal_entity_ids": [row["entity_id"] for row in omitted_reveals],
                    "dm_notes_mode": guidance_mode,
                    "retained_entry_opportunities": retained_opportunities,
                    "retained_public_description": retained_public_description,
                    "registered_encounter_participants": registered_participants.get(key, []),
                }
            )
    return normalizations


class Workflow:
    def __init__(self, store: Store, semantic=None):
        self.store = store
        self.semantic = semantic or SemanticClient(store)
        builder = StateGraph(Address)
        for name in (
            "source",
            "spine",
            "bundles",
            "continuity",
            "assemble",
            "review",
            "repair",
            "publish",
        ):
            builder.add_node(name, getattr(self, name))
        builder.add_edge(START, "source")
        for previous, following in (
            ("source", "spine"),
            ("spine", "bundles"),
            ("bundles", "continuity"),
            ("continuity", "assemble"),
            ("repair", "assemble"),
        ):
            builder.add_edge(previous, following)
        builder.add_conditional_edges(
            "assemble", self.route_assembly, {"repair": "repair", "review": "review"}
        )
        builder.add_conditional_edges(
            "review",
            self.route_review,
            {"repair": "repair", "review": "review", "publish": "publish"},
        )
        builder.add_edge("publish", END)
        self.graph = builder.compile()

    def request(self):
        return self.store.state()["request"]

    def registry(self, bundles=None):
        bundles = self.store.get("bundles") if bundles is None else bundles
        value = records(self.store.get("spine"), bundles)
        value["campaign_spine"] = {
            "kind": "spine",
            "value": {
                k: v for k, v in self.store.get("spine").items() if k not in ("entities", "secrets")
            },
            "chapter_guidance": [
                {"chapter_id": b["chapter_id"], "dm_guidance": b["dm_guidance"]}
                for b in bundles["chapters"]
            ],
        }
        return value

    def dependency_map(self, registry):
        deps = dependencies(registry)
        deps["campaign_spine"].update(
            e["id"]
            for e in self.store.get("spine")["entities"] + self.store.get("spine")["secrets"]
        )
        return deps

    def fingerprint(self, registry, key):
        deps = self.dependency_map(registry)
        needed, pending = set(), list(deps[key])
        while pending:
            ref = pending.pop()
            if ref == key or ref in needed:
                continue
            needed.add(ref)
            pending.extend(deps[ref] - needed)
        spine = self.store.get("spine")
        related = {
            b["chapter_id"]
            for b in self.store.get("bundles")["chapters"]
            if any(
                row["id"] == key
                for field in ("goals", "events", "new_entities", "new_secrets")
                for row in b[field]
            )
        }
        source = self.store.get("source") or {"pages": []}
        pages = {p for c in spine["chapters"] if c["id"] in related for p in c["pages"]}
        if key == "campaign_spine":
            pages.update(p["page"] for p in source["pages"])
        elif "origin" in registry[key]["value"]:
            pages.update(registry[key]["value"]["origin"]["pages"])
        return digest(
            {
                "visual_evidence": {
                    str(p): self.store.get("visual-" + str(p)) for p in sorted(pages)
                },
                "record": registry[key],
                "dependencies": {r: registry[r] for r in sorted(needed)},
                "campaign_context": {k: spine[k] for k in ("premise", "setting", "hook", "ending")},
                "chapter_context": [
                    {
                        "chapter": c,
                        "dm_guidance": next(
                            b["dm_guidance"]
                            for b in self.store.get("bundles")["chapters"]
                            if b["chapter_id"] == c["id"]
                        ),
                    }
                    for c in spine["chapters"]
                    if c["id"] in related
                ],
            }
        )

    async def run(self):
        await self.graph.ainvoke({"slug": self.store.slug}, config={"recursion_limit": sys.maxsize})

    async def source(self, address):
        self.store.enter("source")
        request = self.request()
        if self.store.get("source") is None:
            if request["source_kind"] == "pdf":
                value = await asyncio.to_thread(acquire_pdf, self.store, Path(request["pdf_path"]))
            else:
                value = {
                    "kind": "ai",
                    "sha256": digest(request["generation"]),
                    "request": request["generation"],
                    "pages": [],
                }
            self.store.put("source", value, digest(request))
        source = self.store.get("source")
        if source["kind"] == "pdf":
            candidates = candidate_visual_pages(source)
            self.store.put("visual-candidates", candidates, digest(source))
            self.store.put(
                "visual-diagnostics",
                {
                    "version": VISUAL_CANDIDATE_VERSION,
                    "source_sha256": source.get("sha256"),
                    "pages": {
                        str(p["page"]): visual_page_reasons(p, Path(source["directory"]))
                        for p in source["pages"]
                        if p["page"] in candidates
                    },
                },
                digest(source),
            )
        if self.store.get("mechanics") is None:
            value = await asyncio.to_thread(catalogue, request["rules_edition"])
            self.store.put("mechanics", value, request["rules_edition"])
        return address

    def source_images(self, pages):
        """Use original relevant images in the existing semantic call, never a visual stage."""
        source = self.store.get("source")
        wanted = set(pages) & set(self.store.get("visual-candidates") or [])
        return [
            Path(source["directory"]) / page["image"]
            for page in source.get("pages", [])
            if page["page"] in wanted
        ]

    def map_context(self, chapter_pages):
        """Keep possible maps from the whole chapter, including sparse/uncertain ones.

        Existing visual selection is deliberately conservative: a selected image may
        instead be a stat block. Do not silently discard it because OCR lacks map
        vocabulary. The author/reviewer must inspect the original image itself.
        """
        source = self.store.get("source")
        wanted = set(chapter_pages) & set(self.store.get("visual-candidates") or [])
        return [
            {
                "reference_id": f"source_page_{page['page']:04d}",
                "physical_page": page["page"],
                "source_sha256": source["sha256"],
                "image_sha256": page["image_sha256"],
                "image": page["image"],
                "role": "Original chapter visual; inspect for relevant map geometry.",
            }
            for page in source.get("pages", [])
            if page["page"] in wanted
        ]

    def templates(self, pages=None):
        result = [
            {"name": r["name"], "verified_edition": r["rules_edition"]}
            for r in self.store.get("mechanics")
        ]
        if pages is None:
            return result
        text = " ".join(p["text"] for p in pages).casefold()

        def mentioned(name):
            words = name.casefold().split()
            last = words[-1]
            endings = {last, last + "s", last + "es"}
            if last.endswith("f"):
                endings.add(last[:-1] + "ves")
            if last.endswith("fe"):
                endings.add(last[:-2] + "ves")
            if len(last) > 1 and last.endswith("y") and last[-2] not in "aeiou":
                endings.add(last[:-1] + "ies")
            prefix = r"\s+".join(re.escape(word) for word in words[:-1])
            pattern = (
                (prefix + r"\s+" if prefix else "")
                + "(?:"
                + "|".join(re.escape(word) for word in sorted(endings))
                + ")"
            )
            return re.search(r"(?<!\w)" + pattern + r"(?!\w)", text) is not None

        return {
            "index": result,
            "relevant_source_profiles": [
                r for r in self.store.get("mechanics") if mentioned(r["name"])
            ],
        }

    def source_reading_packets(self, pages):
        """Pack original pages/images against the complete reading request's context."""
        batch = []
        for page in pages:
            proposed = batch + [page]
            images = self.source_images([p["physical_page"] for p in proposed])
            if not request_context(
                "read-source", SourceReading, {"source_pages": proposed}, images
            )["fits"]:
                if not batch:
                    raise NeedsAttention(
                        "One original page exceeds the model context and needs smaller exact source spans."
                    )
                yield batch
                batch = [page]
                images = self.source_images([page["physical_page"]])
                if not request_context(
                    "read-source", SourceReading, {"source_pages": batch}, images
                )["fits"]:
                    raise NeedsAttention(
                        "One original page exceeds the model context and needs smaller exact source spans."
                    )
            else:
                batch = proposed
        if batch:
            yield batch

    async def spine(self, address):
        self.store.enter("spine")
        if self.store.get("spine"):
            self.retain_visual_selection(self.store.get("spine")["important_visual_pages"])
            return address
        source = self.store.get("source")
        pages = evidence_pages(source)
        evidence = {
            "source_kind": source["kind"],
            "request": self.request().get("generation"),
            "preferences": self.request()["preferences"],
            "verified_creatures": self.templates(),
            "source_pages": pages,
            "image_pages": sorted(set(self.store.get("visual-candidates") or [])),
        }
        images = self.source_images([p["page"] for p in source["pages"]])
        original_context = request_context("spine", Spine, evidence, images)
        readings = []
        if not original_context["fits"]:
            for index, packet in enumerate(self.source_reading_packets(pages)):
                value = await self.semantic.call(
                    "source-reading-" + str(index),
                    "read-source",
                    SourceReading,
                    {"source_pages": packet},
                    self.source_images([p["physical_page"] for p in packet]),
                )
                readings.append(value.model_dump())
            evidence["source_readings"] = readings
            evidence.pop("source_pages")
            if not request_context("spine", Spine, evidence, images)["fits"]:
                evidence["source_images_in_readings"] = evidence.pop("image_pages")
                images = []
        self.store.put(
            "spine-context",
            {
                "original_request": original_context,
                "submitted_request": request_context("spine", Spine, evidence, images),
                "source_mode": "reading-packets" if readings else "original-pages",
            },
        )
        value = await self.semantic.call(
            "spine",
            "spine",
            Spine,
            evidence,
            images,
        )
        if source["kind"] == "pdf":
            known_pages = {p["page"] for p in source["pages"]}
            if any(not set(c.pages) <= known_pages for c in value.chapters):
                raise NeedsAttention("Spine cites nonexistent physical pages.")
            if not set(value.important_visual_pages) <= known_pages:
                raise NeedsAttention("Visual evidence request cites nonexistent physical pages.")
            assigned = {p for chapter in value.chapters for p in chapter.pages}
            if assigned != known_pages:
                raise NeedsAttention(
                    "Chapter plan must account for every physical page, including shared appendices: "
                    + str(sorted(known_pages - assigned))
                )
        self.store.put("spine", value.model_dump(), digest(evidence))
        self.retain_visual_selection(value.important_visual_pages)
        return address

    def retain_visual_selection(self, selected):
        source = self.store.get("source")
        spine = self.store.get("spine") or {}
        selected = set(selected) | {
            page
            for row in spine.get("entities", []) + spine.get("secrets", [])
            if row["origin"].get("uncertain")
            for page in row["origin"]["pages"]
        }
        known = {p["page"] for p in source.get("pages", [])}
        if not set(selected) <= known:
            raise NeedsAttention("Visual evidence request cites nonexistent physical pages.")
        candidates = sorted(set(self.store.get("visual-candidates") or []) | set(selected))
        self.store.put("visual-candidates", candidates, digest(source))

    async def bundles(self, address):
        self.store.enter("bundles")
        if self.store.get("bundles"):
            # Derived projection only: immutable paid task/response rows remain
            # untouched, while unfinished retained work gains shared references.
            value = canonical_profiles(self.store.get("bundles"), self.store.get("mechanics") or [])
            if value != self.store.get("bundles"):
                self.store.put("bundles", value)
            return address
        spine, source = self.store.get("spine"), self.store.get("source")
        chapters = sorted(spine["chapters"], key=lambda c: c["play_order"])
        # One chapter per call, with complete original pages and a compact shared dossier.
        items = []
        for chapter in chapters:
            numbers = chapter_source_pages(spine, chapter) if source["kind"] == "pdf" else []
            pages = evidence_pages(source, numbers)
            items.append({"chapter": chapter, "source_evidence": pages, "context_pages": numbers})

        async def author(item):
            chapter = item["chapter"]
            number = chapter["id"]
            dependency_ids = set(chapter.get("depends_on", []))
            accepted = []
            for previous in chapters:
                if previous["id"] not in dependency_ids:
                    continue
                bundle = self.store.get("chapter-" + previous["id"])
                if bundle is not None:
                    accepted.append(
                        {
                            "chapter_id": bundle["chapter_id"],
                            "new_entities": bundle["new_entities"],
                            "new_secrets": bundle["new_secrets"],
                            "goals": [
                                {k: goal[k] for k in ("id", "objective", "completion")}
                                for goal in bundle["goals"]
                            ],
                            "event_connections": [
                                {
                                    k: event[k]
                                    for k in (
                                        "id",
                                        "location_id",
                                        "participants",
                                        "trigger",
                                        "timing",
                                        "next_event_ids",
                                    )
                                }
                                for event in bundle["events"]
                            ],
                            "dm_guidance": bundle["dm_guidance"],
                        }
                    )
            evidence = {
                "campaign": {
                    key: spine[key]
                    for key in ("title", "premise", "setting", "hook", "ending", "dm_guidance")
                },
                "all_chapter_briefs": spine["chapters"],
                "shared_entities": spine["entities"],
                "shared_truths": spine["secrets"],
                "requested_chapter": chapter,
                "source_evidence": item["source_evidence"],
                "source_context_pages": item["context_pages"],
                "source_kind": source["kind"],
                "original_request": source.get("request"),
                "accepted_dependencies": accepted,
                "verified_creatures": self.templates(item["source_evidence"]),
                "preferences": self.request()["preferences"],
                "image_pages": sorted(
                    set(item["context_pages"]) & set(self.store.get("visual-candidates") or [])
                ),
                "map_context": self.map_context(chapter["pages"]),
                "known_mechanical_profiles": [
                    profile
                    for previous in chapters
                    if previous["id"] in dependency_ids
                    for profile in (self.store.get("chapter-" + previous["id"]) or {}).get(
                        "mechanical_profiles", []
                    )
                ],
            }
            dependency = digest(evidence)
            saved = self.store.get("chapter-" + number, dependency)
            if saved is not None:
                return saved
            images = self.source_images(item["context_pages"])
            original_context = request_context("chapters", Bundles, evidence, images)
            # Dense authoring is partitioned into original-page work, never a
            # summary replacing the pages. The assembled chapter has no output cap.
            bundle, authoring_receipt = await author_chapter(self, evidence, images)
            returned_pages = set(bundle["source_pages"])
            assigned_pages = set(chapter["pages"])
            if not returned_pages <= set(item["context_pages"]):
                raise NeedsAttention(
                    "Chapter source-page inventory cites pages outside its supplied evidence."
                )
            # Ownership is a local planner fact, not evidence of semantic completeness.
            # An author may list only narrative pages or include shared appendices.
            # Preserve its inventory as diagnostics; final review checks actual content
            # against ALL assigned original pages, including any omitted inventory rows.
            bundle["source_pages"] = chapter["pages"]
            self.store.put(
                "chapter-context-" + number,
                {
                    "chapter_id": number,
                    "source_pages": [
                        {
                            "page": p["page"],
                            "raw_sha256": p["raw_sha256"],
                            "image_sha256": p["image_sha256"],
                        }
                        for p in source.get("pages", [])
                        if p["page"] in item["context_pages"]
                    ],
                    "shared_context_sha256": digest([spine["entities"], spine["secrets"]]),
                    "dependency_chapters": sorted(dependency_ids),
                    "model_source_pages": sorted(returned_pages),
                    "model_omitted_inventory_pages": sorted(assigned_pages - returned_pages),
                    "authoring": authoring_receipt,
                    "evidence_tokens": token_count(evidence),
                    "original_request_context": original_context,
                    "submitted_request_context": request_context(
                        "chapters", Bundles, evidence, images
                    ),
                },
                dependency,
            )
            self.store.put("chapter-" + number, bundle, dependency)
            return bundle

        # One call per chapter; only real dependencies impose a wave boundary.
        remaining = {item["chapter"]["id"]: item for item in items}
        accepted = {}
        while remaining:
            ready = [
                item
                for item in remaining.values()
                if set(item["chapter"].get("depends_on", [])) <= accepted.keys()
            ]
            if not ready:
                raise NeedsAttention(
                    "Chapter dependency cycle; no additional model work submitted."
                )
            authored = await asyncio.gather(
                *(author(item) for item in ready), return_exceptions=True
            )
            for item, result in zip(ready, authored, strict=True):
                if isinstance(result, BaseException):
                    raise result
                key = item["chapter"]["id"]
                accepted[key] = result
                del remaining[key]
        value = canonical_profiles(
            Bundles(chapters=[accepted[c["id"]] for c in chapters]).model_dump(),
            self.store.get("mechanics") or [],
        )
        self.store.put("bundles", value, digest(spine))
        return address

    async def continuity(self, address):
        self.store.enter("continuity")
        await repair_structure(self)
        spine, bundles = self.store.get("spine"), self.store.get("bundles")
        registry = validate_semantics(spine, bundles, self.store.get("source"))
        # Explicit joins: additions only, no silent last-writer overwrite of shared truth.
        aliases = {}
        for key, row in registry.items():
            if row["kind"] != "entity":
                continue
            for name in [row["value"]["name"], *row["value"]["aliases"]]:
                normalized = " ".join(name.casefold().split())
                if normalized in aliases and aliases[normalized] != key:
                    await self.resolve_identities(spine, bundles, aliases[normalized], key, name)
                    return await self.continuity(address)
                aliases[normalized] = key
        self.store.put(
            "continuity",
            {
                "records": registry,
                "dependencies": {k: sorted(v) for k, v in dependencies(registry).items()},
                "aliases": aliases,
            },
            digest({"spine": spine, "bundles": bundles}),
        )
        return address

    async def resolve_identities(self, spine, bundles, first, second, name):
        registry = records(spine, bundles)
        affected = set(closure(registry, {first, second}))
        evidence = {
            "collision": {"name": name, "ids": [first, second]},
            "affected_records": {k: registry[k] for k in sorted(affected)},
            "chapters": spine["chapters"],
        }
        signature = digest(evidence)
        if signature in (self.store.get("identity-history") or []):
            raise NeedsAttention("Continuity resolution repeated an unchanged collision.")
        result = await self.semantic.call(
            "continuity-join", "continuity", ContinuityPatch, evidence
        )
        replacement = {e.id: e.model_dump() for e in result.entities}
        if (
            result.spine
            or result.secrets
            or result.goals
            or result.events
            or result.chapters
            or result.field_edits
            or result.mechanical_profiles
            or not set(replacement) <= {first, second}
        ):
            raise NeedsAttention("Identity resolution changed unrelated narrative records.")
        redirects = {}
        for join in result.joins:
            if join.canonical_id not in {first, second} or not set(join.duplicate_ids) <= {
                first,
                second,
            } - {join.canonical_id}:
                raise NeedsAttention("Identity join is outside the conflicting identities.")
            redirects.update({key: join.canonical_id for key in join.duplicate_ids})

        def retarget(value):
            if isinstance(value, list):
                return [retarget(v) for v in value]
            if isinstance(value, dict):
                return {
                    k: retarget(v)
                    if k.endswith("_ids") or k.endswith("_id") or isinstance(v, (dict, list))
                    else v
                    for k, v in value.items()
                }
            return redirects.get(value, value) if isinstance(value, str) else value

        for container, field in [
            (spine, "entities"),
            *((b, "new_entities") for b in bundles["chapters"]),
        ]:
            container[field] = [
                replacement.get(e["id"], e) for e in container[field] if e["id"] not in redirects
            ]
        spine, bundles = retarget(spine), retarget(bundles)
        validate_semantics(spine, bundles, self.store.get("source"))
        if digest([spine, bundles]) == digest([self.store.get("spine"), self.store.get("bundles")]):
            raise NeedsAttention("Continuity resolution made no progress.")
        self.store.put("identity-history", (self.store.get("identity-history") or []) + [signature])
        self.store.put("spine", spine)
        self.store.put("bundles", bundles)

    async def assemble(self, address):
        self.store.enter("assemble")
        spine, bundles, mechanics = (
            self.store.get("spine"),
            self.store.get("bundles"),
            self.store.get("mechanics"),
        )
        canonical = canonical_profiles(bundles, mechanics)
        if canonical != bundles:
            self.store.put("bundles", canonical)
            bundles = canonical
        self.store.put(
            "mechanical-registry", bundles.get("mechanical_profiles", []), digest(bundles)
        )
        validate_semantics(spine, bundles, self.store.get("source"))
        local_findings = mechanical_findings(bundles, mechanics)
        self.store.put("assembly-findings", local_findings)
        findings = [f for f in self.store.get("findings") or [] if f["owner"] != "assembly"]
        self.store.put(
            "findings",
            findings + [{"owner": "assembly", "value": finding} for finding in local_findings],
        )
        if local_findings:
            return address
        dep = digest([spine, bundles, mechanics, self.store.get("source")])
        if self.store.get("compiled", dep) is None:
            value = assemble(
                self.store.slug,
                self.request()["rules_edition"],
                spine,
                bundles,
                mechanics,
                source=self.store.get("source"),
            )
            self.store.put("compiled", value, dep)
            atomic_json(self.store.directory / "playable.json", value)
            (self.store.directory / "dm-guide.md").write_text(value["guide"])
        return address

    def route_assembly(self, address):
        return "repair" if self.store.get("assembly-findings") else "review"

    async def review(self, address):
        self.store.enter("review")
        registry = self.registry()
        coverage = self.store.get("review-coverage") or {}
        findings = self.store.get("findings") or []
        current_ids = {
            key for key in registry if coverage.get(key) != self.fingerprint(registry, key)
        }
        # Cross-record findings accompany the pending record as dependency context.
        # Already current coverage must not expand a partial-response continuation.
        compiled, source = self.store.get("compiled"), self.store.get("source")
        bundles_document = self.store.get("bundles")
        bundles = bundles_document["chapters"]
        compiled_scene_ids = {scene["scene_id"] for scene in compiled["manifest"]["scenes"]}
        compiled_bindings = {(row["event_id"], row["actor_id"]) for row in compiled["grounded"]}
        chapter_inventory = [
            {
                "chapter_id": bundle["chapter_id"],
                "content_record_id": derived(bundle["chapter_id"], "content"),
                "goal_ids": [goal["id"] for goal in bundle["goals"]],
                "item_ids": [item["entity_id"] for item in bundle.get("items", [])],
                "check_ids": [check["id"] for check in bundle.get("checks", [])],
                "events": [
                    {
                        "id": event["id"],
                        "title": event["title"],
                        "compiled_scene_present": event["id"] in compiled_scene_ids,
                        "encounters": [
                            {
                                "actor_id": row["actor_id"],
                                "profile_id": row.get("profile_id"),
                                "creature": next(
                                    (
                                        p["creature"]
                                        for p in bundles_document.get("mechanical_profiles", [])
                                        if p["id"] == row.get("profile_id")
                                    ),
                                    row.get("creature", ""),
                                ),
                                "quantity": row["quantity"],
                                "source_stat_block_present": bool(
                                    next(
                                        (
                                            p.get("source_stat_block")
                                            for p in bundles_document.get("mechanical_profiles", [])
                                            if p["id"] == row.get("profile_id")
                                        ),
                                        row.get("source_stat_block"),
                                    )
                                ),
                                "compiled_binding_present": (event["id"], row["actor_id"])
                                in compiled_bindings,
                            }
                            for row in event["encounters"]
                        ],
                    }
                    for event in bundle["events"]
                ],
            }
            for bundle in bundles
        ]
        for packet in packets(
            [{"id": key, **registry[key]} for key in sorted(current_ids)], target=60000
        ):
            ids = [r["id"] for r in packet]
            deps = self.dependency_map(registry)
            context_ids = set().union(*(deps[key] for key in ids)) - set(ids)
            prior = [f["value"] for f in findings if set(ids) & set(f["value"]["record_ids"])]
            context_ids.update(key for f in prior for key in f["record_ids"] if key not in ids)
            complete_chapters = {
                bundle["chapter_id"]
                for bundle in bundles
                if derived(bundle["chapter_id"], "content") in ids
            }
            chapter_records = {
                row["id"]
                for bundle in bundles
                if bundle["chapter_id"] in complete_chapters
                for field in ("events", "goals")
                for row in bundle[field]
            }
            context_ids.update(chapter_records)
            context_ids.update(ref for key in chapter_records for ref in deps[key])
            context_ids.difference_update(ids)
            scene_detail_ids = set(ids) | chapter_records
            relevant_chapters = {
                b["chapter_id"]
                for b in bundles
                if any(e["id"] in ids for e in b["events"])
                or any(g["id"] in ids for g in b["goals"])
                or derived(b["chapter_id"], "content") in ids
            }
            relevant_pages = set(
                p
                for c in self.store.get("spine")["chapters"]
                if c["id"] in relevant_chapters
                for p in chapter_source_pages(self.store.get("spine"), c)
            )
            relevant_pages.update(
                p
                for key in ids
                if key != "campaign_spine"
                for p in registry[key]["value"]["origin"]["pages"]
            )
            if "campaign_spine" in ids:
                relevant_pages.update(p["page"] for p in source["pages"])
            # For a whole-spine packet retain overview evidence; for local review retain only
            # relevant source pages and exact compiled passages, not the full manuscript.
            evidence = {
                "review_ids": ids,
                "previous_findings": prior,
                "records": packet,
                "dependencies": {key: registry[key] for key in sorted(context_ids)},
                "campaign_record_index": {key: row["kind"] for key, row in registry.items()},
                "chapter_inventory": chapter_inventory,
                "review_scope": {
                    "campaign_overview": "campaign_spine" in ids,
                    "complete_chapter_ids": sorted(complete_chapters),
                    "scene_detail_ids": sorted(scene_detail_ids & compiled_scene_ids),
                },
                "chapter_boundaries": self.store.get("spine")["chapters"],
                "compiled_scenes": [
                    s for s in compiled["manifest"]["scenes"] if s["scene_id"] in scene_detail_ids
                ],
                "mechanics": [g for g in compiled["grounded"] if g["event_id"] in scene_detail_ids],
                "mechanical_profiles": {
                    key: value
                    for key, value in compiled.get("mechanical_profiles", {}).items()
                    if key in set(ids) | context_ids
                },
                "source_evidence": evidence_pages(source, relevant_pages),
                "image_pages": sorted(
                    relevant_pages & set(self.store.get("visual-candidates") or [])
                ),
                "map_context": self.map_context(relevant_pages),
                "compiled_objects": compiled["manifest"]["objects"],
                "compiled_checks": compiled["manifest"]["resolution_policies"],
                "source_references": compiled.get("source_references", []),
                "capability_findings": compiled.get("capability_findings", []),
                "review_direction": "Compare original source to output and identify omitted material within the supplied review scope. The complete chapter_inventory and campaign_record_index establish which records exist across all packets. Full chapter scene and encounter details accompany complete_chapter_ids; campaign_overview audits shared continuity. A record outside this packet is not missing. Private original source references do not replace useful authored running material.",
                "visual_evidence": [
                    self.store.get("visual-" + str(n))
                    for n in sorted(relevant_pages)
                    if self.store.get("visual-" + str(n))
                ],
                "public_text": [
                    {
                        "scene_id": s["scene_id"],
                        "text": next(
                            r["value"]["public_description"]
                            for key, r in registry.items()
                            if key == s["scene_id"]
                        ),
                    }
                    for s in compiled["manifest"]["scenes"]
                    if s["scene_id"] in scene_detail_ids
                ],
            }
            value = await self.semantic.call(
                "final-review-" + digest(ids)[:12],
                "review",
                Review,
                evidence,
                self.source_images(relevant_pages),
            )
            for finding in value.findings:
                if not set(finding.record_ids) <= registry.keys():
                    raise NeedsAttention("Critic finding references nonexistent records.")
            requested, returned = set(ids), set(value.covered_ids)
            covered = requested & returned
            fingerprints = {key: self.fingerprint(registry, key) for key in covered}
            progressed = {key for key in covered if coverage.get(key) != fingerprints[key]}
            if returned != requested:
                self.store.put(
                    "review-coverage-reconciled-" + digest(evidence),
                    {
                        "requested_ids": sorted(requested),
                        "returned_ids": value.covered_ids,
                        "accepted_ids": sorted(covered),
                        "missing_ids": sorted(requested - covered),
                        "extra_ids": sorted(returned - requested),
                        "newly_covered_ids": sorted(progressed),
                    },
                    digest(evidence),
                )
            if not progressed:
                raise NeedsAttention(
                    "Independent critic made no coverage progress; retained response requires attention."
                )
            # A partially covered packet cannot clear findings on its missing
            # records, even if the original finding has the same packet owner.
            # A later scoped recheck can finish a cross-record finding once all
            # its records have current exact coverage, with the others as context.
            owner = digest(ids)
            findings = [
                f
                for f in findings
                if not (
                    set(f["value"]["record_ids"]) & covered
                    and all(
                        key in covered or coverage.get(key) == self.fingerprint(registry, key)
                        for key in f["value"]["record_ids"]
                    )
                )
            ]
            findings += [{"owner": owner, "value": f.model_dump()} for f in value.findings]
            coverage.update(fingerprints)
            self.store.put("review-coverage", coverage)
            self.store.put("findings", findings)
        return address

    def route_review(self, address):
        registry, coverage = self.registry(), self.store.get("review-coverage") or {}
        if any(coverage.get(key) != self.fingerprint(registry, key) for key in registry):
            return "review"
        if any(f["value"]["severity"] == "material" for f in self.store.get("findings") or []):
            return "repair"
        return "publish"

    def retained_review_page_sources(self, findings, pages):
        """Trace a supplied finding back to its retained critic's original OCR packet."""
        sources = []
        if not pages:
            return sources
        with self.store.db() as db:
            reviews = list(
                db.execute(
                    """SELECT id, json_extract(response, '$.id') AS response_id,
                          json_extract(accepted, '$.findings') AS findings
                   FROM task WHERE node LIKE 'final-review-%' AND state='accepted'"""
                )
            )
            for review in reviews:
                accepted_findings = json.loads(review["findings"] or "[]")
                owned = [finding["id"] for finding in findings if finding in accepted_findings]
                if not owned:
                    continue
                supplied = {
                    row[0]
                    for row in db.execute(
                        """SELECT json_extract(page.value, '$.physical_page')
                           FROM task, json_each(
                               json_extract(task.request, '$.input[1].content[0].text'),
                               '$.source_evidence') AS page WHERE task.id=?""",
                        (review["id"],),
                    )
                }
                covered = sorted(pages & supplied)
                if covered:
                    sources.append(
                        {
                            "task_id": review["id"],
                            "response_id": review["response_id"],
                            "finding_ids": owned,
                            "pages": covered,
                        }
                    )
        return sources

    async def repair(self, address):
        self.store.enter("repair")
        normalized = canonical_profiles(
            self.store.get("bundles"), self.store.get("mechanics") or []
        )
        registry = self.registry(normalized)
        findings = [
            f["value"] for f in self.store.get("findings") if f["value"]["severity"] == "material"
        ]
        changed = {key for f in findings for key in f["record_ids"]}
        allowed = set(registry) if "campaign_spine" in changed else set(closure(registry, changed))
        evidence = {
            "findings": findings,
            "allowed_ids": sorted(allowed),
            "affected_records": {key: registry[key] for key in sorted(allowed)},
            "context": {
                "spine": self.store.get("spine"),
                "dependencies": {
                    key: sorted(v) for key, v in dependencies(registry).items() if key in allowed
                },
            },
            "verified_creatures": self.templates(),
            "mechanical_profiles": normalized.get("mechanical_profiles", []),
        }
        source_pages = {
            p
            for key in allowed
            if key != "campaign_spine"
            for p in registry[key]["value"]["origin"]["pages"]
        }
        if "campaign_spine" in allowed:
            source_pages.update(p["page"] for p in self.store.get("source")["pages"])
        # A local event's provenance may omit the chapter's map page. Supply those
        # original images as read-only evidence without enlarging the repair closure.
        spine = self.store.get("spine")
        related_chapters = {
            bundle["chapter_id"]
            for bundle in self.store.get("bundles")["chapters"]
            if derived(bundle["chapter_id"], "content") in allowed
            or any(
                row["id"] in allowed
                for field in ("events", "goals", "new_entities", "new_secrets")
                for row in bundle[field]
            )
        }
        chapter_pages = {
            page
            for chapter in spine["chapters"]
            if chapter["id"] in related_chapters or source_pages & set(chapter["pages"])
            for page in chapter["pages"]
        }
        evidence["map_context"] = self.map_context(chapter_pages)
        source_pages.update(row["physical_page"] for row in evidence["map_context"])
        evidence["verified_creatures"] = self.templates(
            evidence_pages(self.store.get("source"), source_pages)
        )
        evidence["source_evidence"] = evidence_pages(self.store.get("source"), source_pages)
        evidence["image_pages"] = sorted(
            source_pages & set(self.store.get("visual-candidates") or [])
        )
        evidence["visual_evidence"] = [
            self.store.get("visual-" + str(n))
            for n in sorted(source_pages)
            if self.store.get("visual-" + str(n))
        ]
        signature = digest(evidence)
        if signature in (self.store.get("repair-history") or []):
            raise NeedsAttention(
                "Semantic repair returned to an unchanged problem; paid loop stopped with findings retained."
            )
        value = await self.semantic.call(
            "scoped-repair", "repair", Patch, evidence, self.source_images(source_pages)
        )
        patches = [
            (kind, row)
            for kind, rows in (
                ("entity", value.entities),
                ("secret", value.secrets),
                ("goal", value.goals),
                ("event", value.events),
            )
            for row in rows
        ]
        if value.spine is not None and "campaign_spine" not in allowed:
            raise NeedsAttention("Repair tried to change the campaign spine outside its closure.")
        patched_events = {e.id: e for e in value.events if e.id in allowed}
        new_entities = {row.id: row for row in value.entities if row.id not in registry}
        for key in new_entities:
            if key in {"campaign_spine", "campaign_registry"} or not any(
                key in e.participants + e.clue_ids + e.visible_entity_ids or key == e.location_id
                for e in patched_events.values()
            ):
                raise NeedsAttention("New repair entity is not used by an affected event.")
        for kind, row in patches:
            if row.id in new_entities and kind == "entity":
                continue
            if row.id in registry and registry[row.id]["kind"] != kind:
                raise NeedsAttention("Repair tried to change a record's kind.")
        # A referenced identity may already exist outside the affected closure.
        # Preserve it locally instead of accepting its incidental re-description or
        # rejecting useful paid repairs. The complete proposal stays in its receipt.
        discarded = [row.id for _, row in patches if row.id not in allowed | new_entities.keys()]
        if discarded:
            self.store.put("repair-preserved-" + signature, {"unchanged_ids": discarded})
        patches = [
            (kind, row)
            for kind, row in patches
            if row.id in allowed or (kind == "entity" and row.id in new_entities)
        ]
        spine, bundles = (
            copy.deepcopy(self.store.get("spine")),
            copy.deepcopy(normalized),
        )
        before = digest([spine, bundles])
        chapter_replacements = {}
        chapter_normalizations = []
        chapter_reconciliations = []
        for bundle in value.chapters:
            key = derived(bundle.chapter_id, "content")
            if key not in allowed or registry[key]["kind"] != "chapter":
                raise NeedsAttention(
                    "Repair tried to replace a chapter outside its affected closure."
                )
            if bundle.chapter_id in chapter_replacements:
                raise NeedsAttention("Chapter repair repeated a chapter proposal.")
            original = next(b for b in bundles["chapters"] if b["chapter_id"] == bundle.chapter_id)
            replacement = bundle.model_dump()
            contained = ("events", "goals", "new_entities", "new_secrets")
            wrong_kinds = [
                {
                    "id": row["id"],
                    "accepted_kind": registry[row["id"]]["kind"],
                    "proposed_kind": kind,
                }
                for field, kind in (
                    ("events", "event"),
                    ("goals", "goal"),
                    ("new_entities", "entity"),
                    ("new_secrets", "secret"),
                )
                for row in replacement[field]
                if row["id"] in registry and registry[row["id"]]["kind"] != kind
            ]
            if wrong_kinds:
                # Older prompts sometimes serialized the supplied kind=chapter
                # running reference as an Event with the chapter-content ID. Do
                # not reinterpret that prose as gameplay or accept its rewrite.
                # Independent valid patches can still recover from the paid task.
                chapter_reconciliations.append(
                    {
                        "chapter_id": bundle.chapter_id,
                        "reason": "Existing record encoded under a different record kind.",
                        "wrong_kind_records": wrong_kinds,
                        "proposal": replacement,
                        "proposal_sha256": digest(replacement),
                        "retained_chapter_sha256": digest(original),
                        "action": "Ignore entire chapter proposal; retain accepted chapter. Independent record patches remain subject to normal validation.",
                    }
                )
                continue
            replacement = canonical_profiles(
                {
                    "chapters": [replacement],
                    "mechanical_profiles": list(
                        (
                            {p["id"]: p for p in bundles.get("mechanical_profiles", [])}
                            | {p.id: p.model_dump() for p in value.mechanical_profiles}
                        ).values()
                    ),
                },
                self.store.get("mechanics") or [],
            )["chapters"][0]
            incomplete = any(
                {row["id"] for row in replacement[field]} < {row["id"] for row in original[field]}
                for field in contained
            )
            normalization = {"chapter_id": bundle.chapter_id, "omitted_record_ids": {}}
            if incomplete:
                proposed_pages = replacement["source_pages"]
                if len(proposed_pages) != len(set(proposed_pages)):
                    raise NeedsAttention("Chapter repair repeated source pages.")
                extra_pages = set(proposed_pages) - set(original["source_pages"])
                supplied_pages = {row["physical_page"] for row in evidence["source_evidence"]}
                physical_pages = {row["page"] for row in self.store.get("source")["pages"]}
                relevant_findings = [
                    finding
                    for finding in findings
                    if key in closure(registry, set(finding["record_ids"]))
                ]
                review_sources = self.retained_review_page_sources(
                    relevant_findings, extra_pages - supplied_pages
                )
                reviewed_pages = {page for item in review_sources for page in item["pages"]}
                if not extra_pages <= (supplied_pages | reviewed_pages) & physical_pages:
                    raise NeedsAttention(
                        "Chapter repair cited nonexistent or unsupplied source pages."
                    )
                normalization["additional_source_pages"] = sorted(extra_pages)
                normalization["retained_review_page_sources"] = review_sources
                normalization["proposed_source_pages"] = proposed_pages
                replacement["source_pages"] = copy.deepcopy(original["source_pages"])
                if not replacement["dm_guidance"]:
                    replacement["dm_guidance"] = original["dm_guidance"]
                    normalization["retained_empty_dm_guidance"] = True
                old_notes, new_notes = original["source_notes"], replacement["source_notes"]
                if not new_notes:
                    replacement["source_notes"] = old_notes
                    normalization["source_notes_mode"] = "retained_omitted_reference"
                elif old_notes and old_notes not in new_notes:
                    replacement["source_notes"] = (
                        "Accepted correction supersedes any conflicting earlier guidance.\n\n"
                        + new_notes
                        + "\n\nRetained earlier chapter reference:\n\n"
                        + old_notes
                    )
                    normalization["source_notes_mode"] = "paid_correction_layer"
                for field, identity_key in (("items", "entity_id"), ("checks", "id")):
                    before_rows = {row[identity_key]: row for row in original[field]}
                    after_rows = {row[identity_key]: row for row in replacement[field]}
                    if (
                        len(after_rows) != len(replacement[field])
                        or after_rows.keys() - before_rows.keys()
                    ):
                        raise NeedsAttention(
                            "Chapter repair changed contained procedure inventory."
                        )
                    normalization["omitted_record_ids"][field] = sorted(
                        before_rows.keys() - after_rows.keys()
                    )
                    replacement[field] = [
                        after_rows.get(row[identity_key], row) for row in original[field]
                    ]
            elif set(replacement["source_pages"]) != set(original["source_pages"]):
                raise NeedsAttention("Chapter repair changed its original source inventory.")
            for field in contained:
                before_rows = {row["id"]: row for row in original[field]}
                after_rows = {row["id"]: row for row in replacement[field]}
                if (
                    len(after_rows) != len(replacement[field])
                    or after_rows.keys() - before_rows.keys()
                ):
                    raise NeedsAttention("Chapter repair changed contained record inventory.")
                if any(
                    before_rows[key] != row for key, row in after_rows.items() if key not in allowed
                ):
                    raise NeedsAttention("Chapter repair changed unrelated contained records.")
                # Chapter content repairs can omit unchanged events/goals as well
                # as actors/secrets. Omission is not deletion authority: retain every record,
                # applying only explicitly supplied changes within the closure.
                replacement[field] = [after_rows.get(row["id"], row) for row in original[field]]
                if incomplete:
                    normalization["omitted_record_ids"][field] = sorted(
                        before_rows.keys() - after_rows.keys()
                    )
            if incomplete:
                chapter_normalizations.append(normalization)
            chapter_replacements[bundle.chapter_id] = replacement
        if chapter_reconciliations:
            self.store.put(
                "repair-reconciled-" + signature,
                {"ignored_chapter_proposals": chapter_reconciliations},
                signature,
            )
        bundles["chapters"] = [
            chapter_replacements.get(b["chapter_id"], b) for b in bundles["chapters"]
        ]
        if value.spine is not None:
            spine = value.spine.model_dump()
            # Chapter membership/identities cannot be silently rewritten by a repair.
            if {c["id"] for c in spine["chapters"]} != {
                c["id"] for c in self.store.get("spine")["chapters"]
            }:
                raise NeedsAttention("Repair changed chapter inventory.")
        replacements = {row.id: row.model_dump() for _, row in patches}
        for field in ("entities", "secrets"):
            spine[field] = [replacements.get(row["id"], row) for row in spine[field]]
        for bundle in bundles["chapters"]:
            for field in ("goals", "events", "new_entities", "new_secrets"):
                bundle[field] = [replacements.get(row["id"], row) for row in bundle[field]]
        definitions = {p["id"]: p for p in bundles.get("mechanical_profiles", [])}
        proposed_definitions = list(value.mechanical_profiles) + [
            p
            for chapter in value.chapters
            for p in chapter.mechanical_profiles
            if chapter.chapter_id in chapter_replacements
        ]
        requested_profiles = {
            encounter.profile_id
            for event in patched_events.values()
            for encounter in event.encounters
        }
        requested_profiles.update(
            encounter.get("profile_id")
            for chapter in chapter_replacements.values()
            for event in chapter["events"]
            if event["id"] in allowed
            for encounter in event["encounters"]
        )
        for profile in proposed_definitions:
            if profile.id in definitions:
                if profile.model_dump() != definitions[profile.id] and profile.id not in allowed:
                    raise NeedsAttention(
                        "Repair changed a shared profile outside its consumer closure."
                    )
            elif profile.id not in requested_profiles:
                raise NeedsAttention("New shared profile is not referenced by an affected event.")
            definitions[profile.id] = profile.model_dump()
        if definitions:
            bundles["mechanical_profiles"] = list(definitions.values())
        for key, row in new_entities.items():
            owner = next(
                b
                for b in bundles["chapters"]
                if any(
                    e["id"] in patched_events
                    and (
                        key in e["participants"] + e["clue_ids"] + e.get("visible_entity_ids", [])
                        or key == e["location_id"]
                    )
                    for e in b["events"]
                )
            )
            owner["new_entities"].append(row.model_dump())
        bundles = canonical_profiles(bundles, self.store.get("mechanics") or [])
        event_normalizations = retain_repair_event_inventory(
            registry, bundles, findings, explicit_event_ids=patched_events
        )
        field_normalizations = apply_field_edits(registry, bundles, value.field_edits, allowed)
        validate_semantics(spine, bundles, self.store.get("source"))
        if digest([spine, bundles]) == before:
            raise NeedsAttention(
                "Repair made no progress; unresolved findings retained without another paid loop."
            )
        if chapter_normalizations or event_normalizations or field_normalizations:
            self.store.put(
                "repair-normalized-" + signature,
                {
                    "chapters": chapter_normalizations,
                    "events": event_normalizations,
                    "field_edits": field_normalizations,
                },
            )
        self.store.put("repair-history", (self.store.get("repair-history") or []) + [signature])
        self.store.put("spine", spine)
        self.store.put("bundles", bundles)
        # Only dependent fingerprints change. Unrelated accepted coverage remains current.
        return address

    async def publish(self, address):
        self.store.enter("publish")
        from .publication import publish

        registry = self.registry()
        coverage = self.store.get("review-coverage") or {}
        if (
            any(coverage.get(key) != self.fingerprint(registry, key) for key in registry)
            or self.route_review(address) == "repair"
        ):
            raise NeedsAttention("Current independent review coverage is incomplete.")
        await asyncio.to_thread(publish, self.store)
        self.store.check()
        self.store.update(status="ready", finished_at=time.time(), error=None)
        return address
