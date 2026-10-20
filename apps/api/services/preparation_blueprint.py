"""Compact semantic authoring; code owns canonical IDs, joins and source offsets.

The blueprint is deliberately separate from an accepted campaign. Its compiler runs
the existing manifest/prepared validators; a successful model response is not Ready.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Literal

from pydantic import Field

from edgar_core.artifacts import text_digest
from edgar_core.grounding import EncounterPlan, EncounterCreatureRequest
from edgar_core.manifests import AdventureManifest, manifest_digest, require_published
from edgar_core.prepared import validate_prepared_campaign
from ingestion.manifest import validate_spans

from .preparation_authoring import ActSupport, ActMechanics, CanonicalDraft, compile_quotes
from .preparation_content import ContentRecord, extraction_for_design


class BlueprintLink(ContentRecord):
    to: str
    requires: dict[str, str] = Field(default_factory=dict)
    private: bool = False
    reveal: dict[str, str] = Field(default_factory=dict)
    sources: list[int] = Field(default_factory=list)


class BlueprintEncounter(ContentRecord):
    creatures: list[EncounterCreatureRequest]
    difficulty: Literal["easy", "medium", "hard", "deadly"] = "deadly"


class BlueprintScene(ContentRecord):
    id: str
    title: str
    sources: list[int]
    public: list[int] = Field(default_factory=list)
    notes: str
    additions: list[str] = Field(default_factory=list)
    links: list[BlueprintLink] = Field(default_factory=list)
    encounters: list[BlueprintEncounter] = Field(default_factory=list)


class BlueprintFact(ContentRecord):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,47}$")
    kind: Literal["actor", "clue", "hint", "hazard", "ending", "object", "objective", "milestone", "clock"]
    title: str
    scenes: list[str]
    sources: list[int]
    # Setup is observable context; sources of a discovery are its gated reveal.
    setup: list[int] = Field(default_factory=list)
    requires: dict[str, str] = Field(default_factory=dict)
    creature: str | None = None
    object: str | None = None
    location: str | None = None
    operations: list[str] = Field(default_factory=list)
    consumable: bool = False
    portable: bool = True
    maximum: int | None = None
    objective: str | None = None


class BlueprintEffect(ContentRecord):
    kind: Literal["discover", "encounter", "objective", "milestone", "clock", "relationship", "acquire", "consume", "transfer", "use", "environment", "move", "learn_place"]
    target: str
    status: str = ""
    sources: list[int]
    operation: str | None = None
    destination: str | None = None
    quantity: int | None = None


class BlueprintApproach(ContentRecord):
    approach: Literal["negotiation", "combat", "avoidance", "failure", "partial_success", "unexpected"]
    action: str
    sources: list[int]
    check: str
    dc: int
    requires: dict[str, str] = Field(default_factory=dict)
    success: list[BlueprintEffect]
    failure: list[BlueprintEffect]
    failure_mode: Literal["partial", "essential_reveal", "cost"]


class BlueprintDecision(ContentRecord):
    objective: str
    scene: str
    approaches: list[BlueprintApproach] = Field(min_length=2)
    addition_rationale: str = ""


class CampaignBlueprint(ContentRecord):
    scenes: list[BlueprintScene]
    facts: list[BlueprintFact]
    decisions: list[BlueprintDecision]
    resource_plan: str
    progression_plan: str


@dataclass(frozen=True)
class SourceSpan:
    id: int
    page: int
    start: int
    end: int
    text: str


def source_spans(extraction):
    """Index exact paragraphs/sentences without changing one source character."""
    spans = []
    for page in extraction.pages:
        boundaries = {0, len(page.text)}
        for match in re.finditer(r"\n\s*\n", page.text):
            boundaries.add(match.end())
        ordered = sorted(boundaries)
        for start, end in zip(ordered, ordered[1:]):
            cuts = [start]
            cuts.extend(start + m.end() for m in re.finditer(
                r"(?<=[.!?])\s+(?=[A-Z\"“])", page.text[start:end]))
            cuts.append(end)
            for first, last in zip(cuts, cuts[1:]):
                text = page.text[first:last]
                if text.strip():
                    spans.append(SourceSpan(len(spans) + 1, page.page, first, last, text))
    return spans


def blueprint_evidence(binding, design, spans):
    return {
        "design": design.model_dump(mode="json"),
        "actual_party": binding.hero_party_snapshot,
        "source": "\n".join(f"[{s.id} p{s.page}] {s.text}" for s in spans),
        "source_span_contract": "Each bracket names an exact physical source span. Cite integer IDs; never repeat source prose.",
    }


AUTHOR_INSTRUCTION = """Prepare the entire campaign as ONE compact semantic blueprint, using the accepted
design and exact PDF spans. Keep all existing scene IDs. Include every scene once with practical
short DM notes: what happens, motives/knowledge, useful interaction, tactics and recovery. Keep
original PDF prose in the source; do not copy it into notes. Fill minor missing details sensibly
in additions, consistent with theme, accepted facts and future acts. Additions are private DM
extensions, never fake source quotations. The original source is retained in full.

Define shared important actors, objects, clues, objectives and clocks ONCE in facts using short
keys. Facts cite source spans and actual scene scopes. Discovery sources are exact secret/reveal
text; setup is separately observable context. Code gates discovery sources behind the earned
discovery flag. Never put a reveal source into scene.public or approach.sources; these are
accessible setup evidence. scene.public selects ONLY immediately player-safe source text. Every
scene needs relevant source access; the opening needs unconditional safe public text. Do not
expose introductions, future rewards, concealed treasure or villain truth as setup. Leave
unselected text private. Source blocks can be shared, but cannot have conflicting disclosure
gates. sources for a fact/action/consequence must actually support its meaning. Known-place
effect sources must explicitly mention the destination. An observed item uses discover on a
clue with object=the existing object key; acquisition is a separate deliberate consequence.
Object location is a scene ID (or character for actual carried gear), never narrative ownership.
Actor creature is an exact MM template name only when applicable; never output statistics.

Links must model actual physical travel and cross-act leads. requires/reveal map CLUE KEYS to
discovered; code allocates flag IDs. Private routes require a concrete earned reveal. Include
source-backed routes connecting every scene from the opening; do not invent linear doors or
teleportation. For missing low-impact travel details use coherent DM additions and explain them.

Use a SMALL number of real durable quests/decisions per act, not a micro-quest for every room.
Each act needs at least one objective and one decision with two materially different approaches.
The first advance_id for each act is its primary objective key; remaining advance_ids can be
milestone keys belonging to it. Each milestone's objective field names its one owning objective.
Use exact supplied advance IDs where appropriate. Decisions
name an objective key and a scene. Each approach supplies the action, supported ability/skill
check name, DC, accessible source context, explicit earned success and failure effects. Do not
substitute an ability check for an entire fight: combat tactics remain in scene notes/encounters;
choose meaningful negotiation/avoidance/investigation opportunities for check policies.
Failure must have a real source-backed cost, essential discovery or actual partial progress;
never fake partial success or a no-op to fill the schema. An essential_reveal failure discovers
a real clue; cost uses a clock/consume/environment effect with a declared operation. Where the
PDF leaves a harmless procedure unspecified, supply a reasonable DM addition with rationale.
Every policy effect needs player-safe physical evidence in its scene. Definitions and evidence
links, policies, branches, outcomes, scene packets, provenance and generated IDs are compiled
by code: do NOT repeat those records. Clock maximum is required. Objects list actual supported
operations when used. Evidence for discovering entities uses the fact's actual reveal sources;
the policy itself cites accessible setup. No invented physical quotes, numeric stats or levels.
Normal rooms and minor checks stay in concise notes; do not enumerate tactical procedures.
Encounters contain exact MM creature names/counts/roles; code grounds them for the actual hero.
Shared resource/progression plans are written once; do not assume the hero has future levels.
Aim for concise, useful content rather than exhaustive serialization. This is not a token cap.
"""


def compile_blueprint(binding, design, blueprint, *, books):
    """Expand semantic decisions without inventing effects, routes or disclosures."""
    extraction = extraction_for_design(binding, design)
    spans = {s.id: s for s in source_spans(extraction)}
    scene_act = {sid: act for act in design.acts for sid in act.scene_ids}
    scene_plans = {s.id: s for s in blueprint.scenes}
    if len(scene_plans) != len(blueprint.scenes) or set(scene_plans) != set(scene_act):
        raise ValueError("Blueprint must contain every accepted scene exactly once.")
    facts = {f.key: f for f in blueprint.facts}
    if len(facts) != len(blueprint.facts):
        raise ValueError("Blueprint fact keys must be unique.")
    advances = {key for act in design.acts for key in act.advance_ids}
    ids = {key: key if key in advances else f"lc_{key}" for key in facts}
    if len(set(ids.values())) != len(ids):
        raise ValueError("Blueprint canonical identities collide.")
    flags = {key: f"lf_{key}" for key, f in facts.items() if f.kind in {"clue", "hint", "hazard", "ending"}}

    def conditions(value):
        unknown = set(value) - flags.keys()
        if unknown:
            raise ValueError(f"Only discovery keys can be predicates: {sorted(unknown)}")
        if any(status != "discovered" for status in value.values()):
            raise ValueError("Discovery predicates must use the emitted status discovered.")
        return {flags[key]: val for key, val in value.items()}

    for fact in blueprint.facts:
        if fact.object is not None and (fact.object not in facts or facts[fact.object].kind != "object"):
            raise ValueError(f"Fact {fact.key}.object must name a declared object key.")
        if not fact.sources or any(number not in spans for number in (*fact.sources, *fact.setup)):
            raise ValueError(f"Fact {fact.key} needs valid source span IDs.")
    for decision in blueprint.decisions:
        if decision.scene not in scene_act:
            raise ValueError(f"Decision scene {decision.scene} is not in the accepted design.")
        for approach in decision.approaches:
            for effect in (*approach.success, *approach.failure):
                targets = scene_act if effect.kind in {"move", "learn_place"} else facts
                if effect.target not in targets:
                    raise ValueError(f"Decision {decision.objective}: unknown {effect.kind} target {effect.target}.")

    pages = dict(extraction.text_pages())
    anchors = {}
    disclosed = {}

    def cite(numbers, scenes=(), *, audience=None, gate=None, entities=(), known=()):
        if not numbers:
            raise ValueError("Semantic records need exact source evidence.")
        result = []
        for number in numbers:
            if number not in spans:
                raise ValueError(f"Unknown exact source span {number}.")
            if set(scenes) - scene_act.keys():
                raise ValueError("Evidence names an unknown scene.")
            span = spans[number]
            anchor = anchors.setdefault(number, {
                "passage_id": f"ls_{number}", "page": span.page, "quote": span.text,
                "scene_ids": set(), "entity_ids": set(), "known_scene_ids": set(),
                "audience": "planner", "requires": {},
                "occurrence": sum(1 for m in re.finditer(re.escape(span.text), pages[span.page])
                                  if m.start() < span.start),
            })
            anchor["scene_ids"].update(scenes)
            anchor["entity_ids"].update(entities)
            anchor["known_scene_ids"].update(known)
            if audience == "player":
                current = conditions(gate) if gate is not None else disclosed.get(number, {})
                if number in disclosed and disclosed[number] != current:
                    raise ValueError(f"Source span {number} has conflicting public disclosure gates.")
                disclosed[number] = current
                anchor.update(audience="player", requires=current)
            result.append(anchor["passage_id"])
        return result

    fragments = {}
    for order, act in enumerate(design.acts, 1):
        fragments[act.act_id] = dict(
            act_id=act.act_id, scenes=[], entities=[], passages=[], objectives=[], milestones=[],
            objects=[], promises=[], clocks=[], opening_journal=[], resolution_policies=[],
            act={"act_id": act.act_id, "title": act.title, "order": order,
                 "scene_ids": act.scene_ids, "provenance": None}, quests=[], scene_packets=[],
            routes=[], branches=[], outcomes=[], extensions=[])
    owner = {}
    page_act = {p: act.act_id for act in design.acts for p in act.source_pages}

    def provenance(numbers, rationale=""):
        if rationale:
            return {"origin": "dm_extension", "passage_ids": [f"ls_{n}" for n in numbers], "rationale": rationale}
        return {"origin": "source", "passage_ids": [f"ls_{n}" for n in numbers]}

    definition_records = {}
    for key, fact in facts.items():
        cite(fact.sources, fact.scenes)
        if not fact.scenes or any(scene not in scene_act for scene in fact.scenes):
            raise ValueError(f"Fact {key} needs valid scene scopes.")
        owner[key] = scene_act[fact.scenes[0]].act_id
        fragment = fragments[owner[key]]
        if fact.kind in {"actor", "clue", "hint", "hazard", "ending"}:
            entity = {"entity_id": ids[key], "kind": fact.kind, "title": fact.title}
            if key in flags:
                entity.update(discovery_flag=flags[key], reveal_on="success")
                cite(fact.sources, fact.scenes, audience="player", gate={**fact.requires, key: "discovered"}, entities=[ids[key]])
            else:
                cite(fact.sources, fact.scenes, entities=[ids[key]])
            if fact.setup:
                cite(fact.setup, fact.scenes, audience="player", gate=fact.requires, entities=[ids[key]])
            if fact.object:
                entity["object_id"] = ids[fact.object]
            if fact.creature:
                mm = books.for_role("mm").binding
                entity["stat_reference"] = {"kind": "template", "creature_name": fact.creature,
                    "collection": mm.collection, "source_sha256": mm.source_sha256,
                    "rules_edition": binding.rules_edition}
            fragment["entities"].append(entity)
        else:
            category = {"object": "objects", "objective": "objectives", "milestone": "milestones", "clock": "clocks"}[fact.kind]
            record = {"canonical_id": ids[key], "title": fact.title, "scene_ids": fact.scenes,
                "passage_ids": cite(fact.sources, fact.scenes), "requires": conditions(fact.requires),
                "statuses": [], "operations": fact.operations, "portable": fact.portable,
                "consumable": fact.consumable, "initial_owner": fact.location}
            if fact.location and fact.location not in scene_act and fact.location != "character":
                raise ValueError(f"Object {key} needs a real initial location, not ownership prose.")
            if fact.kind == "clock":
                if fact.maximum is None or fact.maximum < 1:
                    raise ValueError(f"Clock {key} needs a positive maximum.")
                record["clock_max"] = fact.maximum
            fragment[category].append(record)
            definition_records[key] = record

    defaults = {"discover": "discovered", "encounter": "encountered", "move": "visited",
        "learn_place": "known", "acquire": "acquired", "consume": "consumed", "transfer": "moved",
        "use": "used", "environment": "changed", "clock": "advanced"}

    def effects(rows, scene, *, failure=False):
        compiled = []
        for row in rows:
            target = row.target if row.kind in {"move", "learn_place"} else ids[row.target]
            entity_ids = [target] if row.kind in {"discover", "encounter", "relationship"} else []
            known = [target] if row.kind == "learn_place" else []
            evidence = cite(row.sources, [scene], audience="player",
                gate=({**facts[row.target].requires, row.target: "discovered"}
                      if row.kind == "discover" else None), entities=entity_ids, known=known)
            status = row.status or defaults.get(row.kind)
            if not status:
                raise ValueError(f"{row.kind} needs an explicit intended status.")
            value = {"kind": row.kind, "target_id": target, "status": status, "passage_ids": evidence}
            if row.operation is not None:value["operation"] = row.operation
            if row.destination is not None:value["destination"] = row.destination
            if row.kind == "clock":value["quantity_delta"] = row.quantity or 1
            compiled.append(value)
            if row.target in definition_records:
                record = definition_records[row.target]
                record["statuses"] = sorted(set(record["statuses"]) | {status})
                record["passage_ids"] = sorted(set(record["passage_ids"]) | set(evidence))
                record["scene_ids"] = sorted(set(record["scene_ids"]) | {scene})
                if status == "announced":record["announcement_passage_ids"] = evidence
            if failure and row.kind == "discover":
                next(e for f in fragments.values() for e in f["entities"] if e["entity_id"] == target)["reveal_on"] = "always"
        return compiled

    decisions_by_goal = defaultdict(list)
    for index, decision in enumerate(blueprint.decisions):
        if decision.objective not in facts or facts[decision.objective].kind != "objective":
            raise ValueError("Decision needs an existing objective fact.")
        fragment = fragments[scene_act[decision.scene].act_id]
        quest_id = f"lq_{decision.objective}"
        for position, approach in enumerate(decision.approaches):
            pid, bid = f"lp_{index}_{position}", f"lb_{index}_{position}"
            pids = cite(approach.sources, [decision.scene], audience="player")
            success = effects(approach.success, decision.scene)
            failure = effects(approach.failure, decision.scene, failure=True)
            # A discovery's setup identifies the entity without exposing its reveal.
            for effect in success + failure:
                if effect["kind"] == "discover":
                    for number in approach.sources:anchors[number]["entity_ids"].add(effect["target_id"])
            fragment["resolution_policies"].append({"policy_id": pid, "action": approach.action,
                "scene_ids": [decision.scene], "passage_ids": pids, "mode": "check",
                "check": approach.check, "dc": approach.dc, "requires": conditions(approach.requires),
                "failure_mode": approach.failure_mode, "on_success": success, "on_failure": failure})
            prov = provenance(approach.sources, decision.addition_rationale)
            outcomes = []
            for result, selected, emitted in (("success", "success", success),
                    ("partial" if approach.failure_mode == "partial" else "failure", "failure", failure)):
                oid = f"lo_{index}_{position}_{selected}"
                outcomes.append(oid)
                fragment["outcomes"].append({"outcome_id": oid, "branch_id": bid, "result": result,
                    "policy_selections": [{"policy_id": pid, "result": selected}],
                    "ending_entity_ids": [e["target_id"] for e in emitted if e["kind"] == "discover"
                        and next((f.kind for k,f in facts.items() if ids[k] == e["target_id"]), None) == "ending"],
                    "provenance": prov})
            fragment["branches"].append({"branch_id": bid, "quest_id": quest_id, "scene_id": decision.scene,
                "approach": approach.approach, "action": approach.action, "requires": conditions(approach.requires),
                "policy_ids": [pid], "outcome_ids": outcomes, "provenance": prov})
        decisions_by_goal[decision.objective].append(decision)

    for key, fact in facts.items():
        if fact.kind != "objective":continue
        choices = decisions_by_goal[key]
        if not choices:raise ValueError(f"Objective {key} has no consequential decision.")
        fragment = fragments[owner[key]]
        act = next(a for a in design.acts if a.act_id == owner[key])
        milestones = []
        for milestone_key, milestone in facts.items():
            if milestone.kind != "milestone":continue
            goal = milestone.objective
            if goal is None:
                goals = [k for k,f in facts.items() if f.kind == "objective" and owner[k] == owner[milestone_key]]
                if len(goals) != 1:
                    raise ValueError(f"Milestone {milestone_key} needs its owning objective key.")
                goal = goals[0]
            if goal not in facts or facts[goal].kind != "objective":
                raise ValueError(f"Milestone {milestone_key} has an unknown objective.")
            if goal == key:milestones.append(ids[milestone_key])
        fragment["quests"].append({"quest_id": f"lq_{key}", "objective_id": ids[key],
            "milestone_ids": milestones, "scene_ids": sorted(set(fact.scenes) | {d.scene for d in choices}),
            "required_approaches": sorted({a.approach for d in choices for a in d.approaches}),
            "provenance": provenance(fact.sources)})

    for scene in blueprint.scenes:
        act = scene_act[scene.id]
        fragment = fragments[act.act_id]
        source_ids = cite(scene.sources, [scene.id])
        if scene.public:cite(scene.public, [scene.id], audience="player", gate={})
        links = []
        for link in scene.links:
            if link.to not in scene_act:raise ValueError(f"Unknown route destination {link.to}.")
            link_sources = link.sources or scene.sources
            cite(link_sources, [scene.id])
            links.append({"scene_id": link.to, "requires": conditions(link.requires)})
            fragment["routes"].append({"from_scene_id": scene.id, "to_scene_id": link.to,
                "visibility": "private" if link.private else "known", "traversal_requires": conditions(link.requires),
                "reveal_requires": conditions(link.reveal), "provenance": provenance(link_sources)})
        fragment["scenes"].append({"scene_id": scene.id, "title": scene.title, "links": links})
        packet_sources = set(source_ids)
        for i, text in enumerate(scene.additions):
            eid = f"lx_{scene.id}_{i}"
            fragment["extensions"].append({"extension_id": eid, "passage_id": eid, "scene_ids": [scene.id],
                "audience": "planner", "text": text, "provenance": {"origin": "dm_extension",
                    "rationale": "Thematic connective DM detail supplied for an unspecified procedure; printed source facts remain authoritative."}})
            packet_sources.add(eid)
        fragment["scene_packets"].append({"scene_id": scene.id, "passage_ids": sorted(packet_sources),
            "entity_ids": [ids[k] for k,f in facts.items() if f.kind in {"actor","clue","hint","hazard","ending"} and scene.id in f.scenes],
            "objective_ids": [ids[k] for k,f in facts.items() if f.kind == "objective" and scene.id in f.scenes],
            "milestone_ids": [ids[k] for k,f in facts.items() if f.kind == "milestone" and scene.id in f.scenes],
            "policy_ids": [p["policy_id"] for p in fragment["resolution_policies"] if scene.id in p["scene_ids"]],
            "dm_notes": scene.notes, "provenance": provenance(scene.sources)})

    for key, record in definition_records.items():
        if not record["statuses"]:
            record["statuses"] = {"object": ["acquired","used","changed"], "clock": ["advanced","completed"]}.get(
                facts[key].kind, ["active","completed","failed"])
    for anchor in anchors.values():
        for name in ("scene_ids", "entity_ids", "known_scene_ids"):anchor[name] = sorted(anchor[name])
        fragments[page_act[anchor["page"]]]["passages"].append(anchor)

    compiled = []
    level = binding.hero_party_snapshot["heroes"][0]["level"]
    for act in design.acts:
        fragment = fragments[act.act_id]
        if not fragment["passages"]:raise ValueError(f"Act {act.act_id} needs meaningful source anchors.")
        fragment["act"]["provenance"] = {"origin": "source", "passage_ids": [p["passage_id"] for p in fragment["passages"][:8]]}
        canonical = compile_quotes(binding, design, CanonicalDraft.model_validate(fragment))
        encounters = [EncounterPlan(encounter_id=f"le_{scene.id}_{i}", scene_id=scene.id,
            target_level=level, creatures=encounter.creatures, maximum_difficulty=encounter.difficulty)
            for scene in blueprint.scenes if scene.id in act.scene_ids for i,encounter in enumerate(scene.encounters)]
        support = ActSupport(mechanics=ActMechanics(act_id=act.act_id, encounters=encounters,
            resource_plan=blueprint.resource_plan, progression_plan=blueprint.progression_plan),
            dm_notes="Retain every assigned original PDF page as authoritative. Use these scene notes and explicitly labelled DM additions alongside the exact source.")
        compiled.append((canonical, support))
    fields = {name: [item for f,_ in compiled for item in getattr(f,name)] for name in (
        "scenes","entities","passages","objectives","milestones","objects","promises","clocks","opening_journal","resolution_policies")}
    from edgar_core.prepared import PreparedCampaign
    prepared = PreparedCampaign(source_mode=binding.source_mode, acts=[f.act for f,_ in compiled],
        **{name: [item for f,_ in compiled for item in getattr(f,name)] for name in (
            "quests","scene_packets","routes","branches","outcomes","extensions")})
    manifest = AdventureManifest(source=binding.source_slug, source_sha256=extraction.source_sha256,
        extraction_sha256=extraction.text_sha256(), rules_edition=binding.rules_edition,
        opening_scene_id=design.opening_scene_id, prepared=prepared, **fields)
    report = validate_prepared_campaign(manifest)
    if not report.valid:raise ValueError(str([f.model_dump() for f in report.findings]))
    manifest.reviewed_sha256 = manifest_digest(manifest)
    manifest.reviewed_by = "compact-compiler-v1; independent review pending"
    # This verifies publishability invariants without certifying independent review.
    # The supervisor's separate critic, path and seal gates still control Ready.
    require_published(manifest)
    validate_spans(extraction, manifest)
    return compiled, manifest


def compact_review_batches(manuscript, common, blueprint_artifact, extraction):
    """Present every exact chapter once; omit compiler-owned duplicate joins.

    Canonical effects, disclosures, state/ownership, topology and grounded numbers
    remain available for independent assessment. JSON pointers still refer to the
    complete immutable artifacts, never to reindexed compact arrays.
    """
    canonical = common["canonical"]
    semantic = {key: canonical[key] for key in (
        "source", "opening_scene_id", "entities", "objectives", "milestones", "objects",
        "promises", "clocks", "opening_journal", "resolution_policies")}
    pages = dict(extraction.text_pages())
    semantic["passages"] = [{"json_pointer": f"/manifest/passages/{i}",
        "record": row, "exact_text": pages[row["page"]][row["start"]:row["end"]]}
        for i, row in enumerate(canonical["passages"]) if row["audience"] == "player"]
    prepared = canonical["prepared"]
    semantic["prepared"] = {key: prepared[key] for key in (
        "acts", "quests", "routes", "branches", "outcomes")}
    chapters = [{**chapter.model_dump(mode="json"),
        "text_json_pointer": f"/manuscript/chapters/{i}/text",
        "text_start": 0, "text_end": len(chapter.text), "complete_chapter": True}
        for i, chapter in enumerate(manuscript.chapters)]
    evidence = {**common, "canonical": semantic, "reviewed_chapters": chapters,
        "related_chapter_text": [],
        "blueprint_artifact_id": blueprint_artifact.artifact_id,
        "authored_blueprint": blueprint_artifact.payload["blueprint"],
        "composition": "Every chapter includes all assigned physical source pages verbatim, followed by exact authored scene notes and DM additions. The compiler expands the supplied blueprint into the bound canonical inventory; review all source, notes, gates, effects, routes and grounded encounters here. Repeated ID joins and private source offsets are code-validated."}
    return [(list(range(len(chapters))), evidence)]
