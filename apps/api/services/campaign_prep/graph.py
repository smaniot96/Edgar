"""The new source -> spine -> bundles -> continuity -> assembly -> review graph.

Only this StateGraph dispatches campaign semantic tasks. SQLite is the state authority;
the in-memory graph state is an address, never an independent checkpoint scheduler.
"""

from __future__ import annotations

import asyncio
import copy
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

from .compile import (
    assemble,
    chapter_source_pages,
    closure,
    dependencies,
    derived,
    records,
    validate_semantics,
)
from .mechanical_findings import mechanical_findings
from .mechanics import catalogue
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
            "review", self.route_review, {"repair": "repair", "publish": "publish"}
        )
        builder.add_edge("publish", END)
        self.graph = builder.compile()

    def request(self):
        return self.store.state()["request"]

    def registry(self):
        value = records(self.store.get("spine"), self.store.get("bundles"))
        value["campaign_spine"] = {
            "kind": "spine",
            "value": {
                k: v for k, v in self.store.get("spine").items() if k not in ("entities", "secrets")
            },
            "chapter_guidance": [
                {"chapter_id": b["chapter_id"], "dm_guidance": b["dm_guidance"]}
                for b in self.store.get("bundles")["chapters"]
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
            }
            dependency = digest(evidence)
            saved = self.store.get("chapter-" + number, dependency)
            if saved is not None:
                return saved
            images = self.source_images(item["context_pages"])
            original_context = request_context("chapters", Bundles, evidence, images)
            # Only a complete request exceeding the real model context needs packets.
            # Full original pages stay in every authoring request whenever they fit.
            if not original_context["fits"]:
                readings = []
                for j, packet in enumerate(self.source_reading_packets(item["source_evidence"])):
                    value = await self.semantic.call(
                        f"chapter-evidence-{number}-{j}",
                        "read-source",
                        SourceReading,
                        {"source_pages": packet},
                        self.source_images([p["physical_page"] for p in packet]),
                    )
                    readings.append(value.model_dump())
                evidence["source_readings"] = readings
                evidence.pop("source_evidence")
                if not request_context("chapters", Bundles, evidence, images)["fits"]:
                    evidence["source_images_in_readings"] = evidence.pop("image_pages")
                    images = []
            value = await self.semantic.call(
                "chapter-" + number,
                "chapters",
                Bundles,
                evidence,
                images,
            )
            if len(value.chapters) != 1 or value.chapters[0].chapter_id != number:
                raise NeedsAttention("Authoring response must cover its one requested chapter.")
            bundle = value.chapters[0].model_dump()
            if set(bundle["source_pages"]) != set(chapter["pages"]):
                raise NeedsAttention(
                    "Chapter must retain its complete assigned source-page inventory."
                )
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
        value = Bundles(chapters=[accepted[c["id"]] for c in chapters]).model_dump()
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
        # A recheck owns an issue when any affected record changed. Carry all of its
        # evidence even if the critic initially reported the issue across packet edges.
        for finding in findings:
            if current_ids & set(finding["value"]["record_ids"]):
                current_ids.update(finding["value"]["record_ids"])
        compiled, source = self.store.get("compiled"), self.store.get("source")
        for packet in packets(
            [{"id": key, **registry[key]} for key in sorted(current_ids)], target=60000
        ):
            ids = [r["id"] for r in packet]
            deps = self.dependency_map(registry)
            context_ids = set().union(*(deps[key] for key in ids)) - set(ids)
            prior = [f["value"] for f in findings if set(ids) & set(f["value"]["record_ids"])]
            context_ids.update(key for f in prior for key in f["record_ids"] if key not in ids)
            relevant_chapters = {
                b["chapter_id"]
                for b in self.store.get("bundles")["chapters"]
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
                "chapter_boundaries": self.store.get("spine")["chapters"],
                "compiled_scenes": [
                    s for s in compiled["manifest"]["scenes"] if s["scene_id"] in ids
                ],
                "mechanics": [g for g in compiled["grounded"] if g["event_id"] in ids],
                "source_evidence": evidence_pages(source, relevant_pages),
                "image_pages": sorted(
                    relevant_pages & set(self.store.get("visual-candidates") or [])
                ),
                "compiled_objects": compiled["manifest"]["objects"],
                "compiled_checks": compiled["manifest"]["resolution_policies"],
                "source_references": compiled.get("source_references", []),
                "capability_findings": compiled.get("capability_findings", []),
                "review_direction": "Compare original source to output and identify omitted material, not only defects in existing records. Private original source references are available but authored running material must remain useful and concrete.",
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
                    if s["scene_id"] in ids
                ],
            }
            value = await self.semantic.call(
                "final-review-" + digest(ids)[:12],
                "review",
                Review,
                evidence,
                self.source_images(relevant_pages),
            )
            if set(value.covered_ids) != set(ids):
                raise NeedsAttention("Independent critic did not cover the requested records.")
            for finding in value.findings:
                if not set(finding.record_ids) <= registry.keys():
                    raise NeedsAttention("Critic finding references nonexistent records.")
            # Findings are owned by the coverage packet; recheck replaces that packet only.
            owner = digest(ids)
            findings = [
                f
                for f in findings
                if f["owner"] != owner and not set(f["value"]["record_ids"]) & set(ids)
            ]
            findings += [{"owner": owner, "value": f.model_dump()} for f in value.findings]
            coverage.update({key: self.fingerprint(registry, key) for key in ids})
            self.store.put("review-coverage", coverage)
            self.store.put("findings", findings)
        return address

    def route_review(self, address):
        return (
            "repair"
            if any(f["value"]["severity"] == "material" for f in self.store.get("findings") or [])
            else "publish"
        )

    async def repair(self, address):
        self.store.enter("repair")
        registry = self.registry()
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
        }
        source_pages = {
            p
            for key in allowed
            if key != "campaign_spine"
            for p in registry[key]["value"]["origin"]["pages"]
        }
        if "campaign_spine" in allowed:
            source_pages.update(p["page"] for p in self.store.get("source")["pages"])
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
            copy.deepcopy(self.store.get("bundles")),
        )
        before = digest([spine, bundles])
        chapter_replacements = {}
        for bundle in value.chapters:
            key = derived(bundle.chapter_id, "content")
            if key not in allowed or registry[key]["kind"] != "chapter":
                raise NeedsAttention(
                    "Repair tried to replace a chapter outside its affected closure."
                )
            original = next(b for b in bundles["chapters"] if b["chapter_id"] == bundle.chapter_id)
            if {e["id"] for e in original["events"]} != {e.id for e in bundle.events}:
                raise NeedsAttention("Chapter repair must preserve event identities.")
            if {g["id"] for g in original["goals"]} != {g.id for g in bundle.goals}:
                raise NeedsAttention("Chapter repair must preserve goal identities.")
            replacement = bundle.model_dump()
            if set(replacement["source_pages"]) != set(original["source_pages"]):
                raise NeedsAttention("Chapter repair changed its original source inventory.")
            for field in ("events", "goals", "new_entities", "new_secrets"):
                before_rows = {row["id"]: row for row in original[field]}
                after_rows = {row["id"]: row for row in replacement[field]}
                if after_rows.keys() - before_rows.keys():
                    raise NeedsAttention("Chapter repair changed contained record inventory.")
                if any(
                    before_rows[key] != row for key, row in after_rows.items() if key not in allowed
                ):
                    raise NeedsAttention("Chapter repair changed unrelated contained records.")
                # Chapter content repairs often omit unchanged actors/secrets. An
                # omission is not deletion authority: retain every accepted record,
                # applying only explicitly supplied changes within the closure.
                replacement[field] = [after_rows.get(row["id"], row) for row in original[field]]
            chapter_replacements[bundle.chapter_id] = replacement
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
        validate_semantics(spine, bundles, self.store.get("source"))
        if digest([spine, bundles]) == before:
            raise NeedsAttention(
                "Repair made no progress; unresolved findings retained without another paid loop."
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
