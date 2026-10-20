"""One authoring result per act; exact source compilation stays in code."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from edgar_core.artifacts import text_digest
from edgar_core.assets import AssetRequirement
from edgar_core.grounding import EncounterPlan, MechanicRequirement
from edgar_core.identifiers import Identifier
from edgar_core.manifests import AdventureManifest, Passage, Scene

from .preparation_content import (
    CanonicalAct,
    CompleteChapter,
    ContentRecord,
    ScenePreparation,
    extraction_for_design,
    validate_fragment,
)


class ActMechanics(ContentRecord):
    act_id: str
    encounters: list[EncounterPlan] = Field(default_factory=list, max_length=30)
    mechanics: list[MechanicRequirement] = Field(default_factory=list, max_length=100)
    resource_plan: str = Field(min_length=50, max_length=5000)
    progression_plan: str = Field(min_length=50, max_length=5000)
    level_changes: list[int] = Field(default_factory=list, max_length=20)


class QuotedPassage(ContentRecord):
    """A meaningful exact citation/disclosure, with offsets resolved by code."""

    passage_id: Identifier
    page: int = Field(ge=1)
    quote: str = Field(min_length=1)
    occurrence: int = Field(default=0, ge=0)
    scene_ids: list[Identifier] = Field(default_factory=list)
    entity_ids: list[Identifier] = Field(default_factory=list)
    audience: Literal["player", "planner", "excluded"]
    requires: dict[Identifier, str] = Field(default_factory=dict)
    known_scene_ids: list[Identifier] = Field(default_factory=list)


class CanonicalDraft(CanonicalAct):
    passages: list[QuotedPassage] = Field(min_length=1)


class ActSupport(ContentRecord):
    """Authored once alongside canonical records, then verified/extracted locally."""

    mechanics: ActMechanics
    dm_notes: str = Field(min_length=100)
    # PDFs retain their original chapters. AI campaigns need original full prose.
    chapter_text: str | None = None
    asset_requirements: list[AssetRequirement] = Field(default_factory=list)


class AuthoredAct(ContentRecord):
    canonical: CanonicalDraft
    support: ActSupport


def compile_quotes(binding, design, draft: CanonicalDraft) -> CanonicalAct:
    """Resolve exact anchors and conservatively keep every other character private.

    No source is dropped and no unselected text becomes player-visible. A model
    only selects meaningful citations/disclosures; it never counts characters or
    copies every private paragraph just to prove physical coverage.
    """
    act = next(a for a in design.acts if a.act_id == draft.act_id)
    allowed = set(act.source_pages) if binding.source_mode == "pdf" else {design.acts.index(act) + 1}
    pages = dict(extraction_for_design(binding, design).text_pages())
    selected = []
    identifiers = set()
    for anchor in draft.passages:
        if anchor.page not in allowed or anchor.passage_id in identifiers:
            raise ValueError(f"Invalid or duplicate source anchor {anchor.passage_id} on page {anchor.page}.")
        text = pages[anchor.page]
        start, search = -1, 0
        for _ in range(anchor.occurrence + 1):
            start = text.find(anchor.quote, search)
            if start < 0:
                raise ValueError(f"Quote {anchor.passage_id} is not exact on physical page {anchor.page}.")
            search = start + 1
        selected.append(Passage(
            **anchor.model_dump(exclude={"quote", "occurrence"}),
            start=start, end=start + len(anchor.quote), text_sha256=text_digest(anchor.quote),
        ))
        identifiers.add(anchor.passage_id)
    completed = []
    for page in sorted(allowed):
        cursor = 0
        spans = sorted((p for p in selected if p.page == page), key=lambda p: p.start)
        for passage in spans:
            if passage.start < cursor:
                raise ValueError(f"Source anchors overlap on physical page {page} at {passage.start}.")
            if pages[page][cursor:passage.start].strip():
                completed.append(_private_gap(page, cursor, passage.start, pages[page], act.scene_ids))
            completed.append(passage)
            cursor = passage.end
        if pages[page][cursor:].strip():
            completed.append(_private_gap(page, cursor, len(pages[page]), pages[page], act.scene_ids))
    if len({p.passage_id for p in completed}) != len(completed):
        raise ValueError("Source anchor IDs collide with reserved private source IDs.")
    payload = draft.model_dump()
    payload["passages"] = [p.model_dump() for p in completed]
    fragment = CanonicalAct.model_validate(payload)
    validate_fragment(binding, design, fragment)
    return fragment


def _private_gap(page, start, end, text, scenes):
    return Passage(passage_id=f"source_private_p{page}_{start}_{end}", page=page,
                   start=start, end=end, text_sha256=text_digest(text[start:end]),
                   scene_ids=list(scenes), audience="planner")


def validate_authored_references(binding, design, fragment, artifacts):
    """Validate defined records now, while their owning proposal is correctable.

    Planned future scenes have stable identities but no authored content yet. They
    only stand in for link destinations here; full preparation still validates all
    authored scenes, branches and outcomes before accepting the last act.
    """
    fragments = [CanonicalAct.model_validate(a.payload["fragment"]) for a in artifacts
                 if a.artifact_kind == "canonical_act"] + [fragment]
    fields = {name: [item for f in fragments for item in getattr(f, name)] for name in (
        "scenes", "entities", "passages", "objectives", "milestones", "objects",
        "promises", "clocks", "opening_journal", "resolution_policies")}
    defined_scenes = {s.scene_id for s in fields["scenes"]}
    fields["scenes"] += [Scene(scene_id=scene_id, title=act.title) for act in design.acts
                         for scene_id in act.scene_ids if scene_id not in defined_scenes]
    source = extraction_for_design(binding, design)
    AdventureManifest(source=binding.source_slug, source_sha256=source.source_sha256,
                      extraction_sha256=source.text_sha256(), rules_edition=binding.rules_edition,
                      opening_scene_id=design.opening_scene_id, **fields)


def scene_work(fragment: CanonicalAct, support: ActSupport) -> ScenePreparation:
    return ScenePreparation(act_id=fragment.act_id, packets=fragment.scene_packets,
                            # The canonical fragment already owns its extensions.
                            extensions=[], resource_plan=support.mechanics.resource_plan,
                            unsupported_required_mechanics=[])


def retained_chapter(binding, design, fragment, support):
    if binding.source_mode == "pdf":
        act = next(a for a in design.acts if a.act_id == fragment.act_id)
        pages = extraction_for_design(binding, design).pages
        original = "\n\n".join(
            f"[Physical source page {p.page}]\n{p.text}" for p in pages if p.page in act.source_pages
        )
        notes = "\n\n".join(f"### {packet.scene_id}\n{packet.dm_notes}"
                               for packet in fragment.scene_packets)
        additions = "\n\n".join(
            f"[DM extension {extension.extension_id}; audience={extension.audience}; "
            f"requires={extension.requires}]\n{extension.text}" for extension in fragment.extensions)
        text = f"{original}\n\n[DM preparation notes]\n{support.dm_notes}\n\n{notes}\n\n{additions}"
    else:
        if not support.chapter_text or len(support.chapter_text) < 500:
            raise ValueError("An AI act requires its complete authored chapter text.")
        text = support.chapter_text
    return CompleteChapter(act_id=fragment.act_id, text=text)


def saved_support(artifacts, act_id):
    matches = [a for a in artifacts if a.artifact_kind == "act_authoring"
               and a.scope_keys == (act_id,)]
    if not matches:
        return None
    if len(matches) != 1:
        raise ValueError("An act must have exactly one saved authoring result.")
    return ActSupport.model_validate(matches[0].payload["support"])


def canonical_registry(artifacts):
    """Exact previously accepted definitions for consistent cross-act references."""
    fields = ("entities", "objectives", "milestones", "objects", "promises", "clocks", "resolution_policies")
    return [{"act_id": a.scope_keys[0],
             **{name: a.payload["fragment"].get(name, []) for name in fields},
             "scene_ids": [s["scene_id"] for s in a.payload["fragment"]["scenes"]]}
            for a in artifacts if a.artifact_kind == "canonical_act"]


def source_inventory(binding):
    from .preparation_sources import load_source

    source = load_source(binding)
    return {"extraction_sha256": source.text_sha256(),
            "pages": [{"page": p.page, "text_sha256": text_digest(p.text),
                       "characters": len(p.text)} for p in source.pages if p.text.strip()]}


def grounding_payload(binding, act, support, books, party, *, client=None, actor_manifest=None):
    """Check mechanics while the authoring proposal can still be corrected."""
    from agent.supported_rules import SUPPORTED_MECHANICS
    from edgar_core.grounding import validate_mechanic_requirements

    from .preparation_grounding import ground_prepared_encounters

    plan = support.mechanics
    supported, unsupported = validate_mechanic_requirements(
        plan.mechanics, {m.id for m in SUPPORTED_MECHANICS})
    if (plan.act_id != act.act_id or plan.level_changes or unsupported
            or any(e.scene_id not in act.scene_ids for e in plan.encounters)):
        raise ValueError("Unsupported act, progression, mechanic or encounter scene.")
    from .preparation_sources import load_source

    pages = dict(load_source(binding).text_pages()) if actor_manifest is not None else None
    receipts = ground_prepared_encounters(plan.encounters, party, books, client=client,
        actor_manifest=actor_manifest, source_pages=pages) if plan.encounters else []
    # A named actor is an explicit participant, never inferred from a nearby scene
    # or a generic template name. Adaptation must retain every claimed participant.
    from collections import Counter

    actors = {e.entity_id: e for e in actor_manifest.entities if e.kind == "actor"} if actor_manifest else {}
    stamped = []
    for encounter, receipt in zip(plan.encounters, receipts, strict=True):
        if len(encounter.actor_ids) != len(set(encounter.actor_ids)):
            raise ValueError(f"Encounter {encounter.encounter_id} repeats a named actor.")
        allocated = Counter()
        for actor_id in encounter.actor_ids:
            actor = actors.get(actor_id)
            if actor is None or actor.stat_reference is None:
                raise ValueError(f"Encounter {encounter.encounter_id}: actor {actor_id} has no explicit exact statistics binding.")
            if not any(encounter.scene_id in p.scene_ids and actor_id in p.entity_ids
                       for p in actor_manifest.passages):
                raise ValueError(f"Encounter {encounter.encounter_id}: actor {actor_id} is outside its source-backed scene.")
            ref = actor.stat_reference
            matching = [m for m in receipt.creatures if m.effective_quantity > 0
                and m.actor_id == actor_id
                and m.creature.creature_name.casefold().strip() == ref.creature_name.casefold().strip()
                and ((ref.kind == "template" and m.creature.collection == ref.collection
                      and m.creature.source_sha256 == ref.source_sha256
                      and m.creature.rules_edition == ref.rules_edition)
                     or (ref.kind == "module" and m.creature.collection == actor_manifest.source
                         and m.creature.source_sha256 == actor_manifest.source_sha256
                         and m.creature.extraction_sha256 == actor_manifest.extraction_sha256
                         and m.creature.evidence_origin == "prepared_module"
                         and m.creature.module_evidence is not None
                         and m.creature.module_evidence.actor_id == actor_id))]
            if len(matching) != 1:
                raise ValueError(f"Encounter {encounter.encounter_id}: actor {actor_id} needs its exact source-bound participating creature.")
            member = matching[0]
            member_key = (member.creature.creature_id, member.actor_id)
            allocated[member_key] += 1
            if allocated[member_key] > member.effective_quantity:
                raise ValueError(f"Encounter {encounter.encounter_id}: solo adaptation removed a declared actor; preserve named participants and source encounter groups.")
        stamped.append(receipt.model_copy(update={"actor_ids": list(encounter.actor_ids)}))
    receipts = stamped
    problems = [{"encounter_id": r.encounter_id, "findings": r.findings,
                 "unsupported": [m.model_dump(mode="json") for m in r.unsupported_mechanics]}
                for r in receipts if r.findings or r.unsupported_mechanics]
    if problems:
        raise ValueError(f"Correct the encounter plans using exact grounding diagnostics: {problems}")
    return {"plan": plan.model_dump(mode="json"),
            "encounters": [r.model_dump(mode="json") for r in receipts],
            "supported_mechanics": [m.model_dump(mode="json") for m in supported],
            "book_snapshot_sha256": books.snapshot_sha256,
            "hero_party_sha256": party.party_sha256}


def fragment_for(artifacts, act_id):
    return CanonicalAct.model_validate(next(
        a.payload["fragment"] for a in artifacts
        if a.artifact_kind == "canonical_act" and a.scope_keys == (act_id,)))


def support_for(artifacts, act_id):
    """Legacy accepted fragments stay intact; derive notes from their saved work."""
    support = saved_support(artifacts, act_id)
    if support is not None:
        return support
    fragment = fragment_for(artifacts, act_id)
    grounding = next(a for a in artifacts if a.artifact_kind == "act_grounding"
                     and a.scope_keys == (act_id,))
    return ActSupport(mechanics=ActMechanics.model_validate(grounding.payload["plan"]),
                      dm_notes="\n\n".join(p.dm_notes for p in fragment.scene_packets))


def asset_requirements(artifacts, manifest):
    """Authored requests plus exact clue handouts; topology maps are code-owned."""
    requirements = [r for a in artifacts if a.artifact_kind == "act_authoring"
                    for r in ActSupport.model_validate(a.payload["support"]).asset_requirements]
    by_id = {r.asset_id: r for r in requirements}
    if len(by_id) != len(requirements):
        raise ValueError("Asset request IDs must be globally unique.")
    covered = {p for r in requirements if r.kind == "handout" for p in r.passage_ids}
    for p in manifest.passages:
        if p.audience == "player" and p.requires and p.passage_id not in covered:
            asset_id = "clue_" + text_digest(p.passage_id)[:40]
            requirement = AssetRequirement(asset_id=asset_id, kind="handout", required=True,
                scene_ids=p.scene_ids, passage_ids=[p.passage_id], rationale="Exact gated source clue.")
            if asset_id in by_id:
                raise ValueError("Authored asset collides with a deterministic clue handout.")
            requirements.append(requirement)
            by_id[asset_id] = requirement
    return requirements


def validate_support_assets(binding, design, fragment, support, artifacts):
    """Reject authored asset errors while their owning proposal is correctable."""
    scenes = {s.scene_id for s in fragment.scenes}
    passages = {p.passage_id for p in fragment.passages} | {e.passage_id for e in fragment.extensions}
    entities = {e.entity_id for e in fragment.entities}
    seen = {r.asset_id for a in artifacts if a.artifact_kind == "act_authoring"
            for r in ActSupport.model_validate(a.payload["support"]).asset_requirements}
    extraction = extraction_for_design(binding, design)
    for request in support.asset_requirements:
        if request.asset_id in seen:
            raise ValueError(f"Asset ID {request.asset_id} is already defined.")
        seen.add(request.asset_id)
        if (request.act_id not in (None, fragment.act_id)
                or not set(request.scene_ids) <= scenes
                or not set(request.passage_ids) <= passages
                or request.entity_id is not None and request.entity_id not in entities):
            raise ValueError(f"Asset {request.asset_id} must refer to this act's canonical identities.")
        if request.kind == "generated_illustration" and (
                request.required or not binding.commission.get("image_budget", {}).get("max_image_calls", 0)):
            raise ValueError("Generated decoration must be optional and explicitly commissioned.")
        if request.source_crop is not None:
            crop = request.source_crop
            if (binding.source_mode != "pdf" or crop.source_sha256 != extraction.source_sha256
                    or crop.extraction_sha256 != extraction.text_sha256()
                    or crop.page not in next(a.source_pages for a in design.acts if a.act_id == fragment.act_id)):
                raise ValueError(f"Asset {request.asset_id} has an invalid exact source crop binding.")
