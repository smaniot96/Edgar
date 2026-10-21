"""The new source -> spine -> bundles -> continuity -> assembly -> review graph.

Only this StateGraph dispatches campaign semantic tasks. SQLite is the state authority;
the in-memory graph state is an address, never an independent checkpoint scheduler.
"""

from __future__ import annotations

import asyncio
import copy
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
    VisualEvidence,
)

from .compile import assemble, closure, dependencies, records, validate_semantics
from .mechanical_findings import mechanical_findings
from .mechanics import catalogue
from .provider import SemanticClient
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
            await self.visual(candidates)
        if self.store.get("mechanics") is None:
            value = await asyncio.to_thread(catalogue, request["rules_edition"])
            self.store.put("mechanics", value, request["rules_edition"])
        return address

    async def visual(self, pages):
        source = self.store.get("source")
        for number in sorted(set(pages)):
            page = next((p for p in source["pages"] if p["page"] == number), None)
            if page is None or self.store.get("visual-" + str(number)):
                continue
            evidence = {
                "page": number,
                "ocr": page["text"],
                "regions": page["regions"],
                "quality": page["quality"],
            }
            result = await self.semantic.call(
                "visual-" + str(number),
                "visual",
                VisualEvidence,
                evidence,
                [Path(source["directory"]) / page["image"]],
            )
            if result.page != number:
                raise NeedsAttention("Visual response names the wrong physical page.")
            self.store.put("visual-" + str(number), result.model_dump(), digest(evidence))

    def templates(self):
        return [
            {"name": r["name"], "verified_edition": r["rules_edition"]}
            for r in self.store.get("mechanics")
        ]

    async def spine(self, address):
        self.store.enter("spine")
        if self.store.get("spine"):
            return address
        source = self.store.get("source")
        pages = evidence_pages(source)
        evidence = {
            "source_kind": source["kind"],
            "request": self.request().get("generation"),
            "preferences": self.request()["preferences"],
            "verified_creatures": self.templates(),
        }
        visual_pages = [
            p["page"] for p in source["pages"] if p["quality"]["visual_interpretation_needed"]
        ]
        readings = []
        if token_count(pages) > 60000:
            for index, packet in enumerate(packets(pages, target=30000)):
                value = await self.semantic.call(
                    "source-reading-" + str(index),
                    "read-source",
                    SourceReading,
                    {"source_pages": packet},
                )
                readings.append(value.model_dump())
                visual_pages.extend(value.important_visual_pages)
            evidence["source_readings"] = readings
        else:
            evidence["source_pages"] = pages
        await self.visual(visual_pages)
        evidence["visual_evidence"] = [
            self.store.get("visual-" + str(n)) for n in sorted(set(visual_pages))
        ]
        value = await self.semantic.call("spine", "spine", Spine, evidence)
        if source["kind"] == "pdf":
            known_pages = {p["page"] for p in source["pages"]}
            if any(not set(c.pages) <= known_pages for c in value.chapters):
                raise NeedsAttention("Spine cites nonexistent physical pages.")
            if not set(value.important_visual_pages) <= known_pages:
                raise NeedsAttention("Visual evidence request cites nonexistent physical pages.")
            await self.visual(value.important_visual_pages)
        self.store.put("spine", value.model_dump(), digest(evidence))
        return address

    async def bundles(self, address):
        self.store.enter("bundles")
        if self.store.get("bundles"):
            return address
        spine, source = self.store.get("spine"), self.store.get("source")
        chapters = sorted(spine["chapters"], key=lambda c: c["play_order"])
        # Pack complete chapter evidence; small chapters share a call. Dense chapters keep
        # measured source packets, carrying the same global commitments and registry slice.
        items = []
        for chapter in chapters:
            numbers = chapter["pages"] if source["kind"] == "pdf" else []
            pages = evidence_pages(source, numbers)
            items.append({"chapter": chapter, "source_evidence": pages})
        groups = list(packets(items, target=14000))

        async def author(index, group):
            relevant_entities = set(e for item in group for e in item["chapter"]["entity_ids"])
            relevant_secrets = set(s for item in group for s in item["chapter"]["secret_ids"])
            chapter_ids = {item["chapter"]["id"] for item in group}
            secrets = [
                s
                for s in spine["secrets"]
                if s["id"] in relevant_secrets
                or chapter_ids & set(s["setup_chapters"] + s["reveal_chapters"])
            ]
            relevant_entities.update(e for s in secrets for e in s["known_by"])
            evidence = {
                "campaign": {
                    key: spine[key]
                    for key in ("title", "premise", "setting", "hook", "ending", "dm_guidance")
                },
                "all_chapter_briefs": spine["chapters"],
                "shared_identity_index": [
                    {"id": e["id"], "name": e["name"], "kind": e["kind"], "aliases": e["aliases"]}
                    for e in spine["entities"]
                ],
                "relevant_entities": [e for e in spine["entities"] if e["id"] in relevant_entities],
                "future_truths": secrets,
                "requested_chapters": group,
                "verified_creatures": self.templates(),
                "preferences": self.request()["preferences"],
            }
            numbers = set(n for item in group for n in item["chapter"]["pages"])
            visual = [self.store.get("visual-" + str(n)) for n in sorted(numbers)]
            evidence["visual_evidence"] = [v for v in visual if v]
            # Real context needs determine evidence packets. A dense chapter can be read
            # in packets once, then authored together using those retained readings.
            if token_count(evidence) > 90000:
                reading = []
                for j, packet in enumerate(
                    packets([p for item in group for p in item["source_evidence"]], 30000)
                ):
                    v = await self.semantic.call(
                        f"chapter-evidence-{index}-{j}",
                        "read-source",
                        SourceReading,
                        {"source_pages": packet},
                    )
                    reading.append(v.model_dump())
                evidence["requested_chapters"] = [{"chapter": item["chapter"]} for item in group]
                evidence["source_readings"] = reading
            value = await self.semantic.call(
                "chapters-" + str(index), "chapters", Bundles, evidence
            )
            if {b.chapter_id for b in value.chapters} != chapter_ids or len(value.chapters) != len(
                chapter_ids
            ):
                raise NeedsAttention(
                    "Authoring response did not cover requested chapter inventory."
                )
            self.store.put("chapter-batch-" + str(index), value.model_dump(), digest(evidence))
            return value.chapters

        authored = await asyncio.gather(
            *(author(i, group) for i, group in enumerate(groups)), return_exceptions=True
        )
        for result in authored:
            if isinstance(result, BaseException):
                raise result
        value = Bundles(chapters=[b for batch in authored for b in batch]).model_dump()
        self.store.put("bundles", value, digest(spine))
        return address

    async def continuity(self, address):
        self.store.enter("continuity")
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
        dep = digest([spine, bundles, mechanics])
        if self.store.get("compiled", dep) is None:
            value = assemble(
                self.store.slug, self.request()["rules_edition"], spine, bundles, mechanics
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
            [{"id": key, **registry[key]} for key in sorted(current_ids)], target=15000
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
            }
            relevant_pages = set(
                p
                for c in self.store.get("spine")["chapters"]
                if c["id"] in relevant_chapters
                for p in c["pages"]
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
                "dm_guide": compiled["guide"] if "campaign_spine" in ids else None,
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
                "final-review-" + digest(ids)[:12], "review", Review, evidence
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
        evidence["source_evidence"] = evidence_pages(self.store.get("source"), source_pages)
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
        value = await self.semantic.call("scoped-repair", "repair", Patch, evidence)
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
            if row.id not in allowed or registry[row.id]["kind"] != kind:
                raise NeedsAttention(
                    "Repair tried to change records outside its dependency closure."
                )
        spine, bundles = (
            copy.deepcopy(self.store.get("spine")),
            copy.deepcopy(self.store.get("bundles")),
        )
        before = digest([spine, bundles])
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
