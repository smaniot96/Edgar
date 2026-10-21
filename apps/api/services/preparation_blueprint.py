"""Compact semantic authoring; code owns canonical IDs, joins and source offsets.

The blueprint is deliberately separate from an accepted campaign. Its compiler runs
the existing manifest/prepared validators; a successful model response is not Ready.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Literal

from pydantic import Field

from edgar_core.grounding import EncounterCreatureRequest, EncounterPlan
from edgar_core.manifests import (
    _POLICY_CHECKS,
    AdventureManifest,
    ResolutionPolicy,
    manifest_digest,
    require_published,
)
from edgar_core.prepared import validate_prepared_campaign
from ingestion.manifest import validate_spans

from .preparation_authoring import ActMechanics, ActSupport, CanonicalDraft, compile_quotes
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
    actors: list[str] = Field(default_factory=list)


class BlueprintScene(ContentRecord):
    id: str
    title: str
    sources: list[int]
    public: list[int] = Field(default_factory=list)
    notes: str
    additions: list[str] = Field(default_factory=list)
    links: list[BlueprintLink] = Field(default_factory=list)
    encounters: list[BlueprintEncounter] = Field(default_factory=list)


class BlueprintEngineRequirement(ContentRecord):
    actor: str
    statuses: list[Literal["dead", "defeated", "captive", "released"]] = Field(min_length=1)


class BlueprintFact(ContentRecord):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,47}$")
    kind: Literal["actor", "clue", "hint", "hazard", "ending", "object", "objective", "milestone", "clock"]
    title: str
    name: str | None = None
    scenes: list[str]
    sources: list[int]
    # Setup is observable context; sources of a discovery are its gated reveal.
    setup: list[int] = Field(default_factory=list)
    requires: dict[str, str] = Field(default_factory=dict)
    creature: str | None = None
    stat_sources: list[int] = Field(default_factory=list)
    stat_name: str | None = None
    object: str | None = None
    location: str | None = None
    operations: list[str] = Field(default_factory=list)
    consumable: bool = False
    portable: bool = True
    maximum: int | None = None
    objective: str | None = None
    required: bool = True
    completion_condition: str | None = None
    engine_requirements: list[BlueprintEngineRequirement] = Field(default_factory=list)
    engine_forbidden: list[BlueprintEngineRequirement] = Field(default_factory=list)


class BlueprintEffect(ContentRecord):
    kind: Literal["discover", "encounter", "objective", "milestone", "clock", "relationship", "acquire", "consume", "transfer", "use", "environment", "move", "learn_place"]
    target: str
    status: Literal["", "discovered", "encountered", "visited", "known", "announced", "active",
        "completed", "failed", "advanced", "friendly", "neutral", "hostile", "acquired",
        "consumed", "moved", "used", "changed"] = ""
    sources: list[int]
    operation: str | None = None
    destination: str | None = None
    quantity: int | None = None


class BlueprintFactRequirement(ContentRecord):
    key: str
    statuses: list[Literal["encountered", "completed", "failed", "active", "announced",
        "friendly", "neutral", "hostile", "dead", "defeated", "captive", "released"]]


class BlueprintApproach(ContentRecord):
    approach: Literal["negotiation", "combat", "avoidance", "failure", "partial_success", "unexpected"]
    action: str
    sources: list[int]
    check: str
    dc: int
    requires: dict[str, str] = Field(default_factory=dict)
    requires_facts: list[BlueprintFactRequirement] = Field(default_factory=list)
    success: list[BlueprintEffect]
    failure: list[BlueprintEffect]
    failure_mode: Literal["no_progress", "partial", "essential_reveal", "cost"]


class BlueprintDecision(ContentRecord):
    objective: str
    scene: str
    approaches: list[BlueprintApproach] = Field(min_length=1)
    addition_rationale: str = ""


class BlueprintTransition(ContentRecord):
    scene: str
    action: str
    sources: list[int]
    requires: dict[str, str] = Field(default_factory=dict)
    requires_facts: list[BlueprintFactRequirement] = Field(default_factory=list)
    effects: list[BlueprintEffect]
    addition_rationale: str = ""


class CampaignBlueprint(ContentRecord):
    scenes: list[BlueprintScene]
    facts: list[BlueprintFact]
    decisions: list[BlueprintDecision] = Field(default_factory=list)
    transitions: list[BlueprintTransition] = Field(default_factory=list)
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
        for start, end in zip(ordered, ordered[1:], strict=False):
            cuts = [start]
            cuts.extend(start + m.end() for m in re.finditer(
                r"(?<=[.!?])\s+(?=[A-Z\"“])", page.text[start:end]))
            cuts.append(end)
            for first, last in zip(cuts, cuts[1:], strict=False):
                text = page.text[first:last]
                if text.strip():
                    spans.append(SourceSpan(len(spans) + 1, page.page, first, last, text))
    return spans


def blueprint_evidence(binding, design, spans):
    indexed = []
    previous_page = None
    for span in spans:
        if span.page != previous_page:
            indexed.append(f"[Physical source page {span.page}]")
            previous_page = span.page
        indexed.append(f"[{span.id}] {span.text}")
    return {
        "design": design.model_dump(mode="json"),
        "protagonist_contract": "The hero chosen for each playthrough is always the protagonist. Prepare reusable story content without assuming their identity, class, race, level, statistics or inventory.",
        "source": "\n".join(indexed),
        "source_span_contract": "Each bracket names an exact physical source span. Cite integer IDs; never repeat source prose.",
    }


AUTHOR_INSTRUCTION = """Prepare the entire campaign as ONE compact semantic blueprint, using the accepted
design and exact retained source spans (PDF text or generated campaign seed). Keep all existing scene IDs. Include every scene once with practical
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
Object location is a scene ID, never a preselected hero or assumed carried gear.
Actor name is its exact short identity; title retains private motives/description. Actor creature
is an exact MM template only when the source uses it unchanged. For a source-specific stat block,
use stat_sources: the contiguous integer spans of ONE complete original block, on one physical
page; leave creature null. stat_name is that block's exact printed heading when it differs from
the actor's short name. Never output statistics or substitute generic stats for a named variant.

Links must model actual physical travel and cross-act leads. requires/reveal map CLUE KEYS to
discovered; code allocates flag IDs. Private routes require a concrete earned reveal. Include
source-backed routes connecting every scene from the opening; do not invent linear doors or
teleportation. For missing low-impact travel details use coherent DM additions and explain them.

Define a SMALL number of source-backed objectives and milestones. Each goal's completion_condition
describes the outcome the PDF calls for, not a prescribed sequence of actions. For example, a prisoner
is safely freed when the actual play establishes their freedom; negotiation, stealth, deception,
combat or another credible player solution can achieve it. The live DM judges this from the scene,
source context, committed events and current action. Arrival, an intention or a successful unrelated
check alone does not complete a goal. Goal completion records progress; it cannot manufacture combat
victory, actor HP, inventory or travel. Optional engine_requirements / engine_forbidden use actor keys
and committed statuses only where an explicit combat boundary matters. Do not require a combat
released status for an ordinary narrative rescue. Each milestone's objective field names its owning
objective. Retain all accepted design advance IDs and source-grounded optionality (facts.required).
An act may be connective/setup material without inventing a quest just to satisfy a record count.

Decisions and transitions are OPTIONAL exact engine tools. Include them only when a meaningful
printed mechanic or useful durable action merits one. They are not an exhaustive list of permitted
player actions and need not produce every discovery, objective or milestone. Leave ordinary
conversation, rescue, observations, NPC reactions and creative solutions to the live DM with useful
notes. Do not enumerate success/failure trees to prove the story can finish. If an exact check tool
is included, provide its supported skill/ability and DC, accessible setup, and real consequences;
never replace a fight with a skill check or invent failure costs to fill fields. Only use/environment
effects declare operation; clocks use positive quantity; consume requires a carried consumable.
Tool effects cite their supporting source and preserve earned reveal gates. requires maps clue keys
to discovered; requires_facts is AND across keys and OR across statuses for each key.

Include a source-backed ending as private DM guidance for recognizing the earned resolution.
Keep scene links and secrets faithful to the PDF. Alternatives with the same endpoints are OR;
predicates within one route are AND. Private links enforce every reveal predicate in requires.
Encounters.actors names exact participating actors; creature requests bind each named actor once,
quantity=1, with its exact verified template or full module stat heading. Generic groups remain
distinct. Keep separate rooms as separate encounters. Do not put rescued allies into enemy rosters
to manufacture dispositions. Source statistics and combat outcomes remain engine-owned.
Code compiles definitions, packets, evidence links, optional check tools and canonical IDs;
do not repeat those records. Write reusable resource/progression guidance once, preserving the
source intended level range and progression as source information. The hero chosen at play time
is always the protagonist. Do not assume an existing hero, class, race, level, sheet or inventory.
Keep original encounters and quantities; party fitting happens when an encounter is instantiated.
Keep notes useful and concise. The DM may fill harmless gaps consistently with the theme,
established facts and future acts. Never fabricate source quotes, numeric statistics or levels.
Use supported engine checks only: """ + ", ".join(sorted(_POLICY_CHECKS)) + ".\n"


EFFECT_DEFAULTS = {"discover": "discovered", "encounter": "encountered", "move": "visited",
    "learn_place": "known", "acquire": "acquired", "consume": "consumed", "transfer": "moved",
    "use": "used", "environment": "changed", "clock": "advanced"}


def engine_check(raw):
    """Normalize spelling only; never replace an unsupported mechanic with another."""
    token = re.sub(r"[^a-z0-9]+", "_", raw.lower()).strip("_")
    if token.endswith("_check"):
        token = token[:-6]
    abilities = dict(strength="str", dexterity="dex", constitution="con",
                     intelligence="int", wisdom="wis", charisma="cha")
    if token in abilities:
        token = abilities[token]
    for long, short in abilities.items():
        if token in {f"{long}_save", f"{long}_saving_throw", f"{short}_saving_throw"}:
            token = f"{short}_save"
    token = {"thieves_tool": "thieves_tools", "thieves_tools_proficiency": "thieves_tools"}.get(token, token)
    skills = {"str": ("athletics",), "dex": ("acrobatics", "sleight_of_hand", "stealth", "thieves_tools"),
        "int": ("arcana", "history", "investigation", "nature", "religion"),
        "wis": ("animal_handling", "insight", "medicine", "perception", "survival"),
        "cha": ("deception", "intimidation", "performance", "persuasion")}
    for long, short in abilities.items():
        for skill in skills.get(short, ()):
            if token in {f"{long}_{skill}", f"{short}_{skill}"}:
                token = skill
    return token if token in _POLICY_CHECKS else raw


def display_title(text):
    """Keep labels short; callers retain the complete original in scene notes."""
    if len(text) <= 200:
        return text
    clause = re.split(r";\s|\.\s", text, maxsplit=1)[0]
    if len(clause) <= 200:
        return clause
    return text[:199].rsplit(" ", 1)[0] + "…"


class BlueprintValidationError(ValueError):
    def __init__(self, messages):
        self.diagnostics = [{"type": "BlueprintSemanticError", "message": message,
            "path": message.split(":", 1)[0].split(" ", 1)[0] if message.startswith("/") else None}
            for message in messages]
        super().__init__("Blueprint structural errors:\n" + "\n".join(messages))


def blueprint_preflight(blueprint, design, *, spans=None, template_name=lambda name: name):
    """Collect structural defects before requesting any paid semantic correction."""
    scenes = {sid for act in design.acts for sid in act.scene_ids}
    facts = {f.key: f for f in blueprint.facts}
    errors = []
    if len(facts) != len(blueprint.facts):
        errors.append("/facts: fact keys must be unique")
    actual_scenes = [scene.id for scene in blueprint.scenes]
    if len(actual_scenes) != len(set(actual_scenes)) or set(actual_scenes) != scenes:
        errors.append("/scenes: include every accepted scene exactly once")
    disclosures = {}
    stat_groups = {}
    discovery_keys = {k for k, f in facts.items() if f.kind in {"clue", "hint", "hazard", "ending"}}

    def disclosure(numbers, gate, path):
        for number in numbers:
            if number in disclosures and disclosures[number] != gate:
                errors.append(f"{path}: source span {number} has conflicting public disclosure gates")
            else:
                disclosures[number] = gate

    def predicates(value, path):
        if set(value) - discovery_keys or any(v != "discovered" for v in value.values()):
            errors.append(f"{path} must use declared discovery keys with status discovered")

    def sources(numbers, path, *, required=False):
        if required and not numbers:
            errors.append(f"{path}: exact source evidence is required")
        if spans is not None and set(numbers) - spans.keys():
            errors.append(f"{path}: unknown source span IDs {sorted(set(numbers) - spans.keys())}")

    scene_act = {sid: act.act_id for act in design.acts for sid in act.scene_ids}
    for i, fact in enumerate(blueprint.facts):
        sources(fact.sources, f"/facts/{i}/sources", required=True)
        sources(fact.setup, f"/facts/{i}/setup")
        if not fact.scenes or set(fact.scenes) - scenes:
            errors.append(f"/facts/{i}/scenes: use actual accepted scene scopes")
        if fact.object and (fact.object not in facts or facts[fact.object].kind != "object"):
            errors.append(f"/facts/{i}/object: name a declared object fact")
        if fact.location and fact.location not in scenes and fact.location != "character":
            errors.append(f"/facts/{i}/location: use a scene ID or character for actual carried gear")
        if fact.kind == "clock" and (fact.maximum is None or fact.maximum < 1):
            errors.append(f"/facts/{i}/maximum: clocks need a positive maximum")
        for field in ("engine_requirements", "engine_forbidden"):
            for j, requirement in enumerate(getattr(fact, field)):
                if fact.kind not in {"objective", "milestone"}:
                    errors.append(f"/facts/{i}/{field}: only goals may declare completion boundaries")
                if requirement.actor not in facts or facts[requirement.actor].kind != "actor":
                    errors.append(f"/facts/{i}/{field}/{j}/actor: name a declared actor")
        if fact.kind == "milestone":
            goals = [k for k, f in facts.items() if f.kind == "objective" and f.scenes
                and fact.scenes and scene_act.get(f.scenes[0]) == scene_act.get(fact.scenes[0])]
            if ((fact.objective is None and len(goals) != 1)
                    or (fact.objective is not None and (fact.objective not in facts
                        or facts[fact.objective].kind != "objective"))):
                errors.append(f"/facts/{i}/objective: name this milestone's one owning objective")
        if fact.stat_sources:
            sources(fact.stat_sources, f"/facts/{i}/stat_sources", required=True)
            group = frozenset(fact.stat_sources)
            if any(number in stat_groups and stat_groups[number] != group for number in group):
                errors.append(f"/facts/{i}/stat_sources: overlapping actor stat blocks must use the same complete span group")
            for number in group:
                stat_groups[number] = group
            if spans is not None and not (set(fact.stat_sources) - spans.keys()):
                selected = sorted((spans[n] for n in set(fact.stat_sources)), key=lambda s: s.id)
                if len({s.page for s in selected}) != 1 or any(
                    a.id + 1 != b.id for a, b in zip(selected, selected[1:], strict=False)
                ):
                    errors.append(f"/facts/{i}/stat_sources: include consecutive spans of one complete stat block on one physical page")
                elif fact.stat_name or fact.name:
                    from db.vector.creatures import identified_creature

                    if identified_creature({"stat_block": True, "section": fact.stat_name or fact.name,
                                            "text": "".join(s.text for s in selected)}) is None:
                        errors.append(f"/facts/{i}/stat_sources: select one complete headed source stat block matching stat_name/name, including its original heading, AC, HP and actions")
        predicates(fact.requires, f"/facts/{i}/requires")
        if fact.key in discovery_keys:
            disclosure(fact.sources, {**fact.requires, fact.key: "discovered"}, f"/facts/{i}/sources")
        if fact.kind in {"actor", "clue", "hint", "hazard", "ending"}:
            disclosure(fact.setup, fact.requires, f"/facts/{i}/setup")
    for i, scene in enumerate(blueprint.scenes):
        sources(scene.sources, f"/scenes/{i}/sources", required=True)
        sources(scene.public, f"/scenes/{i}/public")
        disclosure(scene.public, {}, f"/scenes/{i}/public")
        for j, link in enumerate(scene.links):
            if link.to not in scenes:
                errors.append(f"/scenes/{i}/links/{j}/to: use an accepted destination scene")
            sources(link.sources, f"/scenes/{i}/links/{j}/sources")
            predicates(link.requires, f"/scenes/{i}/links/{j}/requires")
            predicates(link.reveal, f"/scenes/{i}/links/{j}/reveal")
            if link.private and not link.reveal:
                errors.append(f"/scenes/{i}/links/{j}/reveal needs the concrete earned discovery for this private route")
            if link.private and any(link.requires.get(k) != v for k, v in link.reveal.items()):
                errors.append(f"/scenes/{i}/links/{j}/requires must enforce every private reveal predicate")
    missing = {key for act in design.acts for key in act.advance_ids} - facts.keys()
    if missing:
        errors.append(f"/facts is missing accepted design advance IDs: {sorted(missing)}")
    targets = {"discover": {"clue", "hint", "hazard", "ending"},
        "encounter": {"actor"}, "relationship": {"actor"}, "objective": {"objective"},
        "milestone": {"milestone"}, "clock": {"clock"},
        **{k: {"object"} for k in ("acquire", "consume", "transfer", "use", "environment")}}
    rows = [(f"/decisions/{i}", decision, False) for i, decision in enumerate(blueprint.decisions)]
    rows += [(f"/transitions/{i}", SimpleNamespace(scene=t.scene, objective=None,
        approaches=[SimpleNamespace(action=t.action, sources=t.sources, requires=t.requires,
            requires_facts=t.requires_facts, success=t.effects, failure=[], check=None, dc=None,
            failure_mode="no_progress")]), True) for i, t in enumerate(blueprint.transitions)]
    for base, decision, automatic in rows:
        if not automatic and (decision.objective not in facts or facts[decision.objective].kind != "objective"):
            errors.append(f"{base}/objective must name an objective fact")
        if decision.scene not in scenes:
            errors.append(f"{base}/scene must name an accepted scene")
        for j, approach in enumerate(decision.approaches):
            path = base if automatic else f"{base}/approaches/{j}"
            sources(approach.sources, path + "/sources", required=True)
            predicates(approach.requires, path + "/requires")
            fact_requirements = []
            for req in approach.requires_facts:
                fact = facts.get(req.key)
                if fact is None or fact.kind not in {"actor", "objective", "milestone"} or not req.statuses:
                    errors.append(f"{path}/requires_facts needs an actor/objective/milestone key and nonempty statuses: {req.key}")
                else:
                    fact_requirements.append({"kind": "entity" if fact.kind == "actor" else fact.kind,
                        "target_id": f"target_{list(facts).index(req.key)}", "statuses": req.statuses})
            outcomes = {}
            for name in ("success", "failure"):
                outcomes[name] = []
                for k, effect in enumerate(getattr(approach, name)):
                    ep = f"{path}/{name}/{k}"
                    sources(effect.sources, ep + "/sources", required=True)
                    if effect.kind in {"move", "learn_place"}:
                        if effect.target not in scenes:
                            errors.append(f"{ep}/target must name an accepted scene")
                    elif effect.target not in facts or facts[effect.target].kind not in targets[effect.kind]:
                        errors.append(f"{ep}/target must name a fact with kind in {sorted(targets[effect.kind])}; got {effect.target}")
                    if effect.kind == "discover" and effect.target in discovery_keys:
                        disclosure(effect.sources, {**facts[effect.target].requires, effect.target: "discovered"}, ep + "/sources")
                    value = dict(kind=effect.kind, target_id="preflight_target",
                        status=effect.status or EFFECT_DEFAULTS.get(effect.kind, ""), passage_ids=["preflight_source"])
                    if effect.operation is not None:
                        value["operation"] = effect.operation
                    if effect.destination is not None:
                        value["destination"] = effect.destination
                    if effect.kind == "clock":
                        value["quantity_delta"] = 1 if effect.quantity is None else effect.quantity
                    if effect.kind in {"use", "environment"} and effect.target in facts:
                        if effect.operation not in facts[effect.target].operations:
                            errors.append(f"{ep}/operation must be declared on target fact {effect.target}")
                    if effect.kind == "transfer" and effect.destination != decision.scene:
                        errors.append(f"{ep}/destination must be the policy's current scene {decision.scene}")
                    if effect.kind == "use" and effect.destination is not None:
                        dest = facts.get(effect.destination)
                        if dest is None or dest.kind != "object" or decision.scene not in dest.scenes:
                            errors.append(f"{ep}/destination must name an object fact present in the policy's scene")
                    # Distinct IDs preserve duplicate-target checking without requiring canonical IDs yet.
                    value["target_id"] = f"target_{list(facts).index(effect.target)}" if effect.target in facts else effect.target
                    outcomes[name].append(value)
            try:
                ResolutionPolicy.model_validate(dict(policy_id="preflight_policy", action=approach.action,
                    scene_ids=["preflight_scene"], passage_ids=["preflight_source"],
                    mode="automatic" if automatic else "check",
                    check=None if automatic else engine_check(approach.check), dc=approach.dc,
                    requires_facts=fact_requirements,
                    failure_mode=approach.failure_mode,
                    on_automatic=outcomes["success"] if automatic else [],
                    on_success=[] if automatic else outcomes["success"], on_failure=outcomes["failure"]))
            except ValueError as exc:
                detail = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}"
                    for e in exc.errors(include_url=False)) if hasattr(exc, "errors") else str(exc)
                errors.append(f"{path}: {detail}")
    if not any(f.kind == "ending" for f in blueprint.facts):
        errors.append("/facts needs a source-backed campaign ending for the DM to recognize")
    for i, fact in enumerate(blueprint.facts):
        if fact.kind == "actor" and not fact.name:
            errors.append(f"/facts/{i}/name needs the exact short actor identity; keep the full description in title (private DM notes)")
        if fact.creature and fact.stat_sources:
            errors.append(f"/facts/{i} must choose an exact MM creature template OR its original module stat_sources")
    for i, scene in enumerate(blueprint.scenes):
        for j, encounter in enumerate(scene.encounters):
            actor_requests = [r.actor_id for r in encounter.creatures if r.actor_id]
            if len(actor_requests) != len(set(actor_requests)) or len(encounter.actors) != len(set(encounter.actors)):
                errors.append(f"/scenes/{i}/encounters/{j}: a named actor may participate only once in one encounter")
            for request in encounter.creatures:
                if request.actor_id and request.actor_id not in encounter.actors:
                    errors.append(f"/scenes/{i}/encounters/{j}/creatures actor_id {request.actor_id} must also be in actors")
                if request.actor_id and request.quantity != 1:
                    errors.append(f"/scenes/{i}/encounters/{j}/creatures: a named actor request needs quantity 1")
                actor = facts.get(request.actor_id)
                if actor and actor.kind == "actor":
                    expected = actor.stat_name or actor.name if actor.stat_sources else actor.creature
                    if expected and template_name(request.creature_name).casefold().strip() != template_name(expected).casefold().strip():
                        errors.append(f"/scenes/{i}/encounters/{j}/creatures: actor {request.actor_id} needs its exact statistics name {expected!r}")
            for key in encounter.actors:
                fact = facts.get(key)
                if fact is None or fact.kind != "actor" or scene.id not in fact.scenes or not (fact.creature or fact.stat_sources):
                    errors.append(f"/scenes/{i}/encounters/{j}/actors needs exact stat-bound actors in this scene; invalid {key}")
                elif sum(r.actor_id == key for r in encounter.creatures) != 1:
                    errors.append(f"/scenes/{i}/encounters/{j}/creatures: named actor {key} needs exactly one explicit actor_id on its participating creature request")
    if errors:
        raise BlueprintValidationError(errors)


def compile_blueprint(binding, design, blueprint, *, books):
    """Expand semantic decisions without inventing effects, routes or disclosures."""
    from db.vector.creature_sources import canonical_source_creature_name

    mm = books.for_role("mm").binding

    def template_name(name):
        return canonical_source_creature_name(name, source_sha256=mm.source_sha256,
                                               rules_edition=binding.rules_edition)

    extraction = extraction_for_design(binding, design)
    spans = {s.id: s for s in source_spans(extraction)}
    blueprint_preflight(blueprint, design, spans=spans, template_name=template_name)
    # A module stat block is one exact contiguous source record. Its original
    # span IDs remain valid authoring references, all resolving to the same anchor.
    span_aliases = {}
    for fact in blueprint.facts:
        if not fact.stat_sources:
            continue
        selected = [spans[n] for n in sorted(set(fact.stat_sources)) if n in spans]
        if len(selected) != len(set(fact.stat_sources)) or len({s.page for s in selected}) != 1:
            raise ValueError(f"Actor {fact.key} needs one complete module stat block on a physical page.")
        first, last = selected[0], selected[-1]
        covered = [s for s in spans.values() if s.page == first.page and first.start <= s.start <= last.start]
        if {s.id for s in covered} != set(fact.stat_sources):
            raise ValueError(f"Actor {fact.key}.stat_sources must cover consecutive source spans.")
        if any(s.id in span_aliases and span_aliases[s.id] != first.id for s in covered):
            raise ValueError("Module stat block ranges overlap with different actor records.")
        span_aliases.update({s.id: first.id for s in covered})
        page = next(p for p in extraction.pages if p.page == first.page)
        spans[first.id] = SourceSpan(first.id, first.page, first.start, last.end, page.text[first.start:last.end])
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

    def fact_conditions(rows):
        return [{"kind": "entity" if facts[r.key].kind == "actor" else facts[r.key].kind,
                 "target_id": ids[r.key], "statuses": r.statuses} for r in rows]

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
        for number in dict.fromkeys(span_aliases.get(n, n) for n in numbers):
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
    page_act = ({p: act.act_id for act in design.acts for p in act.source_pages}
                if binding.source_mode == "pdf" else
                {page: act.act_id for page, act in enumerate(design.acts, 1)})

    def provenance(numbers, rationale=""):
        # Source access keeps every anchor in its owning packet/definition. A
        # provenance record only needs representative exact citations (schema: 32).
        numbers = list(dict.fromkeys(span_aliases.get(n, n) for n in numbers))[:32]
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
            entity = {"entity_id": ids[key], "kind": fact.kind,
                      "title": fact.name if fact.kind == "actor" else display_title(fact.title)}
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
                entity["stat_reference"] = {"kind": "template", "creature_name": template_name(fact.creature),
                    "collection": mm.collection, "source_sha256": mm.source_sha256,
                    "rules_edition": binding.rules_edition}
            elif fact.stat_sources:
                pids = cite(fact.stat_sources, fact.scenes, entities=[ids[key]])
                entity["stat_reference"] = {"kind": "module", "creature_name": fact.stat_name or fact.name,
                                             "passage_id": pids[0]}
            fragment["entities"].append(entity)
        else:
            category = {"object": "objects", "objective": "objectives", "milestone": "milestones", "clock": "clocks"}[fact.kind]
            record = {"canonical_id": ids[key], "title": display_title(fact.title), "scene_ids": fact.scenes,
                "passage_ids": cite(fact.sources, fact.scenes), "requires": conditions(fact.requires),
                "statuses": [], "operations": fact.operations, "portable": fact.portable,
                "consumable": fact.consumable, "initial_owner": fact.location}
            if fact.location and fact.location not in scene_act and fact.location != "character":
                raise ValueError(f"Object {key} needs a real initial location, not ownership prose.")
            if fact.kind == "clock":
                if fact.maximum is None or fact.maximum < 1:
                    raise ValueError(f"Clock {key} needs a positive maximum.")
                record["clock_max"] = fact.maximum
            if fact.kind in {"objective", "milestone"}:
                # Older paid proposals express the outcome in their title/source. Retain that
                # work rather than reauthoring it just to populate a new semantic field.
                record["completion_condition"] = fact.completion_condition or fact.title
                for field in ("engine_requirements", "engine_forbidden"):
                    record[field] = [{"actor_id": ids[r.actor], "statuses": r.statuses}
                                     for r in getattr(fact, field)]
            fragment[category].append(record)
            definition_records[key] = record

    def effects(rows, scene, *, failure=False):
        compiled = []
        for row in rows:
            target = row.target if row.kind in {"move", "learn_place"} else ids[row.target]
            entity_ids = [target] if row.kind in {"discover", "encounter", "relationship"} else []
            known = [target] if row.kind == "learn_place" else []
            evidence = cite(row.sources, [scene], audience="player",
                gate=({**facts[row.target].requires, row.target: "discovered"}
                      if row.kind == "discover" else None), entities=entity_ids, known=known)
            status = row.status or EFFECT_DEFAULTS.get(row.kind)
            if not status:
                raise ValueError(f"{row.kind} needs an explicit intended status.")
            value = {"kind": row.kind, "target_id": target, "status": status, "passage_ids": evidence}
            if row.operation is not None:
                value["operation"] = row.operation
            if row.destination is not None:
                value["destination"] = ids[row.destination] if row.kind == "use" else row.destination
            if row.kind == "clock":
                value["quantity_delta"] = 1 if row.quantity is None else row.quantity
            compiled.append(value)
            if row.target in definition_records:
                record = definition_records[row.target]
                record["statuses"] = sorted(set(record["statuses"]) | {status})
                record["passage_ids"] = sorted(set(record["passage_ids"]) | set(evidence))
                record["scene_ids"] = sorted(set(record["scene_ids"]) | {scene})
                if status == "announced":
                    record["announcement_passage_ids"] = evidence
            if failure and row.kind == "discover":
                next(e for f in fragments.values() for e in f["entities"] if e["entity_id"] == target)["reveal_on"] = "always"
        return compiled

    decisions_by_goal = defaultdict(list)
    for index, decision in enumerate(blueprint.decisions):
        if decision.objective not in facts or facts[decision.objective].kind != "objective":
            raise ValueError("Decision needs an existing objective fact.")
        fragment = fragments[scene_act[decision.scene].act_id]
        for position, approach in enumerate(decision.approaches):
            pid = f"lp_{index}_{position}"
            pids = cite(approach.sources, [decision.scene], audience="player")
            success = effects(approach.success, decision.scene)
            failure = effects(approach.failure, decision.scene, failure=True)
            # A discovery's setup identifies the entity without exposing its reveal.
            for effect in success + failure:
                if effect["kind"] == "discover":
                    for number in approach.sources:
                        anchors[span_aliases.get(number, number)]["entity_ids"].add(effect["target_id"])
            fragment["resolution_policies"].append({"policy_id": pid, "action": approach.action,
                "scene_ids": [decision.scene], "passage_ids": pids, "mode": "check",
                "check": engine_check(approach.check), "dc": approach.dc, "requires": conditions(approach.requires),
                "requires_facts": fact_conditions(approach.requires_facts),
                "failure_mode": approach.failure_mode, "on_success": success, "on_failure": failure})
        decisions_by_goal[decision.objective].append(decision)

    for index, transition in enumerate(blueprint.transitions):
        fragment = fragments[scene_act[transition.scene].act_id]
        pids = cite(transition.sources, [transition.scene], audience="player")
        emitted = effects(transition.effects, transition.scene)
        for effect in emitted:
            if effect["kind"] == "discover":
                for number in transition.sources:
                    anchors[span_aliases.get(number, number)]["entity_ids"].add(effect["target_id"])
        fragment["resolution_policies"].append({"policy_id": f"lt_{index}",
            "action": transition.action, "scene_ids": [transition.scene], "passage_ids": pids,
            "mode": "automatic", "requires": conditions(transition.requires),
            "requires_facts": fact_conditions(transition.requires_facts), "on_automatic": emitted})

    for key, fact in facts.items():
        if fact.kind != "objective":
            continue
        choices = decisions_by_goal[key]
        fragment = fragments[owner[key]]
        milestones = []
        required_milestones = []
        for milestone_key, milestone in facts.items():
            if milestone.kind != "milestone":
                continue
            goal = milestone.objective
            if goal is None:
                goals = [k for k,f in facts.items() if f.kind == "objective" and owner[k] == owner[milestone_key]]
                if len(goals) != 1:
                    raise ValueError(f"Milestone {milestone_key} needs its owning objective key.")
                goal = goals[0]
            if goal not in facts or facts[goal].kind != "objective":
                raise ValueError(f"Milestone {milestone_key} has an unknown objective.")
            if goal == key:
                milestones.append(ids[milestone_key])
                if milestone.required:
                    required_milestones.append(ids[milestone_key])
        fragment["quests"].append({"quest_id": f"lq_{key}", "objective_id": ids[key],
            "milestone_ids": milestones, "scene_ids": sorted(set(fact.scenes) | {d.scene for d in choices}),
            "required_for_completion": fact.required,
            "required_milestone_ids": required_milestones,
            "required_approaches": [],
            "provenance": provenance(fact.sources)})

    for scene in blueprint.scenes:
        act = scene_act[scene.id]
        fragment = fragments[act.act_id]
        source_ids = cite(scene.sources, [scene.id])
        if scene.public:
            cite(scene.public, [scene.id], audience="player", gate={})
        links = []
        for link in scene.links:
            if link.to not in scene_act:
                raise ValueError(f"Unknown route destination {link.to}.")
            link_sources = link.sources or scene.sources
            cite(link_sources, [scene.id])
            links.append({"scene_id": link.to, "requires": conditions(link.requires)})
            fragment["routes"].append({"from_scene_id": scene.id, "to_scene_id": link.to,
                "visibility": "private" if link.private else "known", "traversal_requires": conditions(link.requires),
                "reveal_requires": conditions(link.reveal), "provenance": provenance(link_sources)})
        fragment["scenes"].append({"scene_id": scene.id, "title": display_title(scene.title), "links": links})
        packet_sources = set(source_ids)
        for i, text in enumerate(scene.additions):
            eid = f"lx_{scene.id}_{i}"
            fragment["extensions"].append({"extension_id": eid, "passage_id": eid, "scene_ids": [scene.id],
                "audience": "planner", "text": text, "provenance": {"origin": "dm_extension",
                    "rationale": "Thematic connective DM detail supplied for an unspecified procedure; printed source facts remain authoritative."}})
            packet_sources.add(eid)
        fragment["scene_packets"].append({"scene_id": scene.id, "passage_ids": sorted(packet_sources),
            "entity_ids": [ids[k] for k,f in facts.items() if f.kind in {"actor","clue","hint","hazard","ending"} and scene.id in f.scenes],
            "objective_ids": [ids[k] for k,f in facts.items() if f.kind == "objective"
                              and scene.id in definition_records[k]["scene_ids"]],
            "milestone_ids": [ids[k] for k,f in facts.items() if f.kind == "milestone"
                              and scene.id in definition_records[k]["scene_ids"]],
            "policy_ids": [p["policy_id"] for p in fragment["resolution_policies"] if scene.id in p["scene_ids"]],
            "dm_notes": "\n\n".join([scene.notes]
                + ([scene.title] if display_title(scene.title) != scene.title else [])
                + [fact.title for fact in blueprint.facts
                   if scene.id in fact.scenes and (fact.kind == "actor" or display_title(fact.title) != fact.title)]),
            "provenance": provenance(scene.sources)})

    for key, record in definition_records.items():
        if facts[key].kind in {"objective", "milestone"}:
            # Optional tool effects cannot narrow the live DM's goal lifecycle.
            record["statuses"] = sorted(set(record["statuses"]) | {"active", "completed", "failed"})
        if not record["statuses"]:
            record["statuses"] = {"object": ["acquired","used","changed"], "clock": ["advanced","completed"]}.get(
                facts[key].kind, ["active","completed","failed"])
    for anchor in anchors.values():
        for name in ("scene_ids", "entity_ids", "known_scene_ids"):
            anchor[name] = sorted(anchor[name])
        fragments[page_act[anchor["page"]]]["passages"].append(anchor)
    opening_evidence = [a["passage_id"] for a in anchors.values()
        if a["audience"] == "player" and not a["requires"] and design.opening_scene_id in a["scene_ids"]]
    if opening_evidence:
        fragments[scene_act[design.opening_scene_id].act_id]["opening_journal"].append({
            "kind": "visit", "target_id": design.opening_scene_id, "status": "visited",
            "passage_ids": opening_evidence[:1]})

    compiled = []
    commission = binding.content_commission
    level = (commission.get("starting_level") or commission.get("target_level")) if binding.source_mode == "ai" else None
    for act in design.acts:
        fragment = fragments[act.act_id]
        if not fragment["passages"]:
            raise ValueError(f"Act {act.act_id} needs meaningful source anchors.")
        fragment["act"]["provenance"] = {"origin": "source", "passage_ids": [p["passage_id"] for p in fragment["passages"][:8]]}
        canonical = compile_quotes(binding, design, CanonicalDraft.model_validate(fragment))
        encounters = [EncounterPlan(encounter_id=f"le_{scene.id}_{i}", scene_id=scene.id,
            target_level=level, creatures=[r.model_copy(update={
                "actor_id": ids[r.actor_id] if r.actor_id else None,
                "creature_name": r.creature_name if r.actor_id and facts[r.actor_id].stat_sources
                    else template_name(r.creature_name)}) for r in encounter.creatures],
            maximum_difficulty=encounter.difficulty,
            actor_ids=[ids[k] for k in encounter.actors])
            for scene in blueprint.scenes if scene.id in act.scene_ids for i,encounter in enumerate(scene.encounters)]
        support = ActSupport(mechanics=ActMechanics(act_id=act.act_id, encounters=encounters,
            resource_plan=blueprint.resource_plan, progression_plan=blueprint.progression_plan),
            dm_notes="Use the complete retained source, scene notes and explicitly labelled DM additions. The live DM narrates and recognizes earned progress from actual play.",
            chapter_text=(None if binding.source_mode == "pdf" else "\n\n".join([
                f"# {act.title}\n{act.summary}", act.seed_text,
                *[f"### {packet.scene_id}\n{packet.dm_notes}" for packet in canonical.scene_packets],
                *[f"[DM addition]\n{extension.text}" for extension in canonical.extensions],
                blueprint.resource_plan, blueprint.progression_plan,
            ])))
        compiled.append((canonical, support))
    fields = {name: [item for f,_ in compiled for item in getattr(f,name)] for name in (
        "scenes","entities","passages","objectives","milestones","objects","promises","clocks","opening_journal","resolution_policies")}
    from edgar_core.prepared import PreparedCampaign
    prepared = PreparedCampaign(source_mode=binding.source_mode, narrative_mode="agent",
        acts=[f.act for f,_ in compiled],
        **{name: [item for f,_ in compiled for item in getattr(f,name)] for name in (
            "quests","scene_packets","routes","branches","outcomes","extensions")})
    manifest = AdventureManifest(source=binding.source_slug, source_sha256=extraction.source_sha256,
        extraction_sha256=extraction.text_sha256(), rules_edition=binding.rules_edition,
        opening_scene_id=design.opening_scene_id, prepared=prepared, **fields)
    report = validate_prepared_campaign(manifest)
    if not report.valid:
        raise ValueError(str([f.model_dump() for f in report.findings]))
    manifest.reviewed_sha256 = manifest_digest(manifest)
    manifest.reviewed_by = "compact-compiler-v1; independent review pending"
    # This verifies publishability invariants without certifying independent review.
    # The supervisor's source-scaffold review and publication seal still control Ready.
    require_published(manifest)
    from .preparation_sources import page_budget

    validate_spans(extraction, manifest, degraded_page_budget=page_budget(binding.source_mode))
    return compiled, manifest


def compact_review_batches(manuscript, common, blueprint_artifact, extraction, *, target_tokens=90000,
                           reviewed_ranges=None):
    """Review exact chapters through the sole lean semantic batching path."""
    from .preparation_review_batches import review_batches

    return review_batches(manuscript, common, blueprint_artifact, extraction,
                          target_tokens=target_tokens, reviewed_ranges=reviewed_ranges)
