"""Reviewed adventure structure; source text stays in its extraction bundle and index.

This is a review receipt, not authentication. Reviewers explicitly classify exact spans;
neither heading detection nor a successful vector search certifies player-safe material.
Campaign progress remains in Session.current_scene_id and WorldFlag rows.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from edgar_core.artifacts import text_digest
from edgar_core.identifiers import Digest, Identifier
from edgar_core.prepared import CommittedPreparedFact, PreparedCampaign
from edgar_core.sources import SourceEdition


class ManifestError(ValueError):
    """The source needs explicit manifest review before module play can proceed."""


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Link(Record):
    scene_id: Identifier
    requires: dict[Identifier, str] = Field(default_factory=dict)


class Scene(Record):
    scene_id: Identifier
    title: str = Field(min_length=1, max_length=200)
    links: list[Link] = Field(default_factory=list)
    references: list[Identifier] = Field(default_factory=list)


class StatReference(Record):
    """An explicitly reviewed actor identity, never inferred from its name or prose."""

    kind: Literal["template", "module"]
    creature_name: str = Field(min_length=1, max_length=200)
    collection: str | None = Field(default=None, min_length=1)
    source_sha256: Digest | None = None
    rules_edition: SourceEdition | None = None
    passage_id: Identifier | None = None

    @model_validator(mode="after")
    def explicit_binding(self):
        if self.kind == "template":
            if (
                not self.collection
                or not self.source_sha256
                or self.rules_edition not in ("2014", "2024")
                or self.passage_id
            ):
                raise ValueError(
                    "Templates require a collection, source checksum and explicit edition."
                )
        elif not self.passage_id or any((self.collection, self.source_sha256, self.rules_edition)):
            raise ValueError("Module statistics require a passage in this reviewed manifest.")
        return self


class LairReference(Record):
    """An exact, separately reviewed passage containing one actor's lair actions."""

    creature_name: str = Field(min_length=1, max_length=200)
    passage_id: Identifier


class Entity(Record):
    entity_id: Identifier
    kind: Literal["actor", "clue", "hint", "hazard", "ending"]
    title: str = Field(min_length=1, max_length=200)
    discovery_flag: Identifier | None = None
    reveal_on: Literal["success", "always"] = "success"
    stat_reference: StatReference | None = None
    lair_reference: LairReference | None = None
    # Surprise is an authored encounter fact, never inferred from narration or prose.
    surprise: Literal["actor", "hero"] | None = None
    object_id: Identifier | None = None


class Passage(Record):
    passage_id: Identifier
    page: int = Field(ge=1)
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    text_sha256: Digest | None = None
    scene_ids: list[Identifier] = Field(default_factory=list)
    entity_ids: list[Identifier] = Field(default_factory=list)
    audience: Literal["unreviewed", "player", "planner", "excluded"] = "unreviewed"
    requires: dict[Identifier, str] = Field(default_factory=dict)
    # Explicit mentioned-place identities. Learning these does not visit or move there.
    known_scene_ids: list[Identifier] = Field(default_factory=list)


class ClockReveal(Record):
    """None withholds a field; {} allows it once the clock fact is earned.

    Nonempty predicates are evaluated against committed session flags, never model prose.
    Each disclosure is independent: knowing a current value need not reveal its maximum.
    """

    current_requires: dict[Identifier, str] | None = None
    maximum_requires: dict[Identifier, str] | None = None
    completion_requires: dict[Identifier, str] | None = None


class GoalEngineRequirement(Record):
    """Optional combat-state boundary; never a script for resolving a story goal."""

    actor_id: Identifier
    statuses: list[Literal["dead", "defeated", "captive", "released"]] = Field(min_length=1)


class StoryDefinition(Record):
    """Optional explicitly reviewed story identity; never automatically player knowledge."""

    canonical_id: Identifier
    title: str = Field(min_length=1, max_length=200)
    scene_ids: list[Identifier] = Field(min_length=1)
    passage_ids: list[Identifier] = Field(min_length=1)
    requires: dict[Identifier, str] = Field(default_factory=dict)
    statuses: list[str] = Field(default_factory=lambda: ["active", "completed"])
    reveal_on: Literal["success", "always"] = "success"
    clock_max: int | None = Field(default=None, ge=1, le=1000)
    operations: list[Identifier] = Field(default_factory=list)
    consumable: bool = False
    portable: bool = True
    initial_owner: str | None = Field(default=None, max_length=200)
    announcement_passage_ids: list[Identifier] = Field(default_factory=list)
    clock_reveal: ClockReveal | None = None
    # Source describes the achievement; the live DM judges when play earns it.
    completion_condition: str | None = None
    engine_requirements: list[GoalEngineRequirement] = Field(default_factory=list)
    engine_forbidden: list[GoalEngineRequirement] = Field(default_factory=list)


class OpeningJournalSeed(Record):
    """Explicit reviewed facts earned with the first opening, not the private plan."""

    kind: Literal["discover", "encounter", "visit", "learn_place", "objective", "milestone"]
    target_id: Identifier
    status: Literal[
        "discovered",
        "encountered",
        "visited",
        "known",
        "announced",
        "active",
        "completed",
        "failed",
    ]
    passage_ids: list[Identifier] = Field(min_length=1, max_length=12)


_POLICY_CATEGORIES = {
    "object": "objects",
    "objective": "objectives",
    "milestone": "milestones",
    "clock": "clocks",
}
_POLICY_TARGET_KIND = {
    "move": "scene",
    "learn_place": "scene",
    "discover": "entity",
    "encounter": "entity",
    "relationship": "entity",
    "acquire": "object",
    "consume": "object",
    "transfer": "object",
    "use": "object",
    "environment": "object",
    "objective": "objective",
    "milestone": "milestone",
    "clock": "clock",
}
_POLICY_CHECKS = {
    "str",
    "dex",
    "con",
    "int",
    "wis",
    "cha",
    "athletics",
    "acrobatics",
    "sleight_of_hand",
    "stealth",
    "arcana",
    "history",
    "investigation",
    "nature",
    "religion",
    "animal_handling",
    "insight",
    "medicine",
    "perception",
    "survival",
    "deception",
    "intimidation",
    "performance",
    "persuasion",
    "str_save",
    "dex_save",
    "con_save",
    "int_save",
    "wis_save",
    "cha_save",
    "thieves_tools",
}


class PolicyEffect(Record):
    """A reviewed, source-backed effect the engine may commit for one outcome."""

    kind: Literal[
        "discover",
        "encounter",
        "objective",
        "milestone",
        "clock",
        "relationship",
        "acquire",
        "consume",
        "transfer",
        "use",
        "environment",
        "move",
        "learn_place",
    ]
    target_id: Identifier
    status: Literal[
        "discovered",
        "encountered",
        "visited",
        "known",
        "announced",
        "active",
        "completed",
        "failed",
        "advanced",
        "friendly",
        "neutral",
        "hostile",
        "acquired",
        "consumed",
        "moved",
        "used",
        "changed",
    ]
    passage_ids: list[Identifier] = Field(min_length=1)
    # Clock effects express an increment; other kinds leave this unset.
    quantity_delta: int | None = Field(default=None, ge=1, le=10)
    destination: str | None = Field(default=None, max_length=200)
    operation: Identifier | None = None

    @model_validator(mode="after")
    def valid_effect_shape(self):
        if self.kind not in {"transfer", "use"} and self.destination is not None:
            raise ValueError(f"{self.kind} effects cannot declare a destination.")
        if self.kind == "transfer" and not self.destination:
            raise ValueError("Transfer effects need an explicit destination scene.")
        if self.kind in {"use", "environment"} and not self.operation:
            raise ValueError(f"{self.kind} effects need an explicit reviewed operation.")
        if self.kind not in {"use", "environment"} and self.operation is not None:
            raise ValueError("Only use and environment effects can declare an operation.")
        return self


class PolicyFactRequirement(Record):
    """A committed canonical state; all records must hold, any listed status may hold."""

    kind: Literal["entity", "objective", "milestone"]
    target_id: Identifier
    statuses: list[str] = Field(min_length=1, max_length=12)

    @model_validator(mode="after")
    def supported_statuses(self):
        allowed = (
            {"discovered", "encountered", "friendly", "neutral", "hostile", "captive", "dead", "defeated", "released"}
            if self.kind == "entity"
            else {"announced", "active", "completed", "failed"}
        )
        if len(self.statuses) != len(set(self.statuses)) or not set(self.statuses) <= allowed:
            raise ValueError("Fact prerequisites need unique statuses supported by their kind.")
        return self


def policy_fact_requirements_met(
    requirements: Iterable[PolicyFactRequirement], facts: Iterable[CommittedPreparedFact]
) -> bool:
    """Evaluate canonical facts already scoped to one exact collection/source/manifest."""
    available = {(fact.kind, fact.target_id, fact.status) for fact in facts}
    return all(
        any((required.kind, required.target_id, status) in available for status in required.statuses)
        for required in requirements
    )


class ResolutionPolicy(Record):
    """Reviewed action policy. The model selects a policy ID; the engine owns its mechanics."""

    policy_id: Identifier
    action: str = Field(min_length=1, max_length=300)
    scene_ids: list[Identifier] = Field(min_length=1)
    passage_ids: list[Identifier] = Field(min_length=1)
    requires: dict[Identifier, str] = Field(default_factory=dict)
    requires_facts: list[PolicyFactRequirement] = Field(default_factory=list, max_length=100)
    requires_items: list[str] = Field(default_factory=list, max_length=8)
    mode: Literal["check", "automatic"]
    check: Identifier | None = None
    dc: int | None = Field(default=None, ge=1, le=50)
    failure_mode: Literal["no_progress", "partial", "essential_reveal", "cost"] = "no_progress"
    on_automatic: list[PolicyEffect] = Field(default_factory=list, max_length=20)
    on_success: list[PolicyEffect] = Field(default_factory=list, max_length=20)
    on_failure: list[PolicyEffect] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def valid_resolution(self):
        fact_targets = {(row.kind, row.target_id) for row in self.requires_facts}
        if len(fact_targets) != len(self.requires_facts):
            raise ValueError("Combine alternative prerequisite statuses for the same canonical fact.")
        if self.mode == "check":
            if not self.check or self.dc is None or self.on_automatic:
                raise ValueError("Check policies need a check and DC, but no automatic effects.")
            if self.check not in _POLICY_CHECKS:
                raise ValueError("Resolution policy names an unsupported engine check.")
        elif (
            self.check is not None
            or self.dc is not None
            or not self.on_automatic
            or self.on_success
            or self.on_failure
            or self.failure_mode != "no_progress"
        ):
            raise ValueError("Automatic policies need only automatic effects and no check or DC.")
        for outcomes in (self.on_automatic, self.on_success, self.on_failure):
            targets = {(effect.kind, effect.target_id) for effect in outcomes}
            if len(targets) != len(outcomes):
                raise ValueError("A policy outcome cannot repeat the same kind and target.")
            if sum(effect.kind == "move" for effect in outcomes) > 1:
                raise ValueError("A policy outcome can move to only one destination per action.")
        effects = self.on_automatic + self.on_success + self.on_failure
        if self.failure_mode == "no_progress" and self.on_failure:
            raise ValueError("No-progress failure policies cannot declare failure effects.")
        if self.failure_mode in {"partial", "essential_reveal", "cost"} and not self.on_failure:
            raise ValueError("Partial, reveal or cost failure policies need authored effects.")
        if self.failure_mode == "essential_reveal" and not any(
            effect.kind == "discover" for effect in self.on_failure
        ):
            raise ValueError("Essential-reveal failure policies must reveal an authored entity.")
        if self.failure_mode == "cost" and not any(
            effect.kind in {"clock", "consume", "environment"} for effect in self.on_failure
        ):
            raise ValueError("Cost failure policies need a supported clock or object consequence.")
        for effect in effects:
            allowed_statuses = {
                "discover": {"discovered"},
                "encounter": {"encountered"},
                "relationship": {"friendly", "neutral", "hostile"},
                "move": {"visited"},
                "learn_place": {"known"},
                "objective": {"announced", "active", "completed", "failed"},
                "milestone": {"announced", "active", "completed", "failed"},
                "clock": {"advanced", "completed"},
                "acquire": {"acquired"},
                "consume": {"consumed"},
                "transfer": {"moved"},
                "use": {"used"},
                "environment": {"changed"},
            }[effect.kind]
            if effect.status not in allowed_statuses:
                raise ValueError("Resolution effect has a status unsupported for its kind.")
            if effect.kind == "clock" and effect.quantity_delta is None:
                raise ValueError("Clock consequences need an explicit bounded increment.")
            if effect.kind != "clock" and effect.quantity_delta is not None:
                raise ValueError("Only clock effects can have a quantity delta.")
        return self


def _policy_effects(policy: ResolutionPolicy) -> list[PolicyEffect]:
    return policy.on_automatic + policy.on_success + policy.on_failure


class AdventureManifest(Record):
    manifest_version: Literal[1] = 1
    source: str = Field(min_length=1)
    source_sha256: Digest
    extraction_sha256: Digest
    rules_edition: SourceEdition = "unspecified"
    opening_scene_id: Identifier | None = None
    scenes: list[Scene] = Field(min_length=1)
    entities: list[Entity] = Field(default_factory=list)
    passages: list[Passage] = Field(min_length=1)
    objects: list[StoryDefinition] = Field(default_factory=list)
    objectives: list[StoryDefinition] = Field(default_factory=list)
    milestones: list[StoryDefinition] = Field(default_factory=list)
    promises: list[StoryDefinition] = Field(default_factory=list)
    clocks: list[StoryDefinition] = Field(default_factory=list)
    opening_journal: list[OpeningJournalSeed] = Field(default_factory=list, max_length=20)
    resolution_policies: list[ResolutionPolicy] = Field(default_factory=list)
    # The optional private preparation extension preserves old receipts when absent.
    prepared: PreparedCampaign | None = None
    reviewed_sha256: Digest | None = None
    reviewed_by: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def references_exist(self):
        scenes = {s.scene_id for s in self.scenes}
        entities = {e.entity_id for e in self.entities}
        passages = {p.passage_id for p in self.passages}
        if (
            len(scenes) != len(self.scenes)
            or len(entities) != len(self.entities)
            or len(passages) != len(self.passages)
        ):
            raise ValueError("Canonical scene, entity and passage IDs must be unique.")
        if self.opening_scene_id is not None and self.opening_scene_id not in scenes:
            raise ValueError("Opening scene is not a canonical scene.")
        flags = [e.discovery_flag for e in self.entities if e.discovery_flag]
        if len(flags) != len(set(flags)):
            raise ValueError("Each discovery flag must identify one canonical entity.")
        for scene in self.scenes:
            if any(link.scene_id not in scenes for link in scene.links):
                raise ValueError("Branch link names an unknown scene.")
            if not set(scene.references) <= passages:
                raise ValueError("Cross reference names an unknown passage.")
        for passage in self.passages:
            if passage.end <= passage.start:
                raise ValueError("Passage span is empty or reversed.")
            if not set(passage.scene_ids) <= scenes or not set(passage.entity_ids) <= entities:
                raise ValueError("Passage names an unknown scene or entity.")
            if not set(passage.known_scene_ids) <= scenes:
                raise ValueError("Known place names an unknown scene.")
            if passage.audience not in ("excluded", "unreviewed") and not passage.scene_ids:
                raise ValueError("Playable passages need an explicit scene scope.")
        for category in (
            self.objects,
            self.objectives,
            self.milestones,
            self.promises,
            self.clocks,
        ):
            if len({d.canonical_id for d in category}) != len(category):
                raise ValueError("Story definition IDs must be unique within their kind.")
            for definition in category:
                if (
                    not set(definition.scene_ids) <= scenes
                    or not set(definition.passage_ids) <= passages
                ):
                    raise ValueError("Story definition names an unknown scene or passage.")
                if not set(definition.announcement_passage_ids) <= set(definition.passage_ids):
                    raise ValueError("Announcement evidence must belong to its definition.")
                if (
                    definition.announcement_passage_ids
                    and category is not self.objectives
                    and category is not self.milestones
                ):
                    raise ValueError("Only goals and milestones can be announced.")
                if definition.clock_reveal is not None and category is not self.clocks:
                    raise ValueError("Only clocks can declare clock disclosure.")
                if not definition.statuses or any(
                    status
                    not in {
                        "discovered",
                        "encountered",
                        "visited",
                        "active",
                        "announced",
                        "completed",
                        "failed",
                        "fulfilled",
                        "broken",
                        "advanced",
                        "friendly",
                        "neutral",
                        "hostile",
                        "acquired",
                        "consumed",
                        "moved",
                        "used",
                        "changed",
                    }
                    for status in definition.statuses
                ):
                    raise ValueError("Story definition has unsupported statuses.")
        if len({p.policy_id for p in self.resolution_policies}) != len(self.resolution_policies):
            raise ValueError("Resolution policy IDs must be unique within an adventure.")
        entities_by_id = {entity.entity_id: entity for entity in self.entities}
        for definition in (*self.objectives, *self.milestones):
            for requirement in (*definition.engine_requirements, *definition.engine_forbidden):
                actor = entities_by_id.get(requirement.actor_id)
                if actor is None or actor.kind != "actor":
                    raise ValueError("Goal engine constraints must name an existing actor.")
        definitions = {
            kind: {item.canonical_id: item for item in getattr(self, category)}
            for kind, category in _POLICY_CATEGORIES.items()
        }
        for policy in self.resolution_policies:
            if not set(policy.scene_ids) <= scenes or not set(policy.passage_ids) <= passages:
                raise ValueError("Resolution policy names an unknown scene or passage.")
            if not any(
                passage.scene_ids and set(passage.scene_ids).intersection(policy.scene_ids)
                for passage in self.passages
                if passage.passage_id in policy.passage_ids
            ):
                raise ValueError("Resolution policy evidence must belong to one of its scenes.")
            for required in policy.requires_facts:
                if required.kind == "entity":
                    entity = entities_by_id.get(required.target_id)
                    if entity is None:
                        raise ValueError("Resolution prerequisite names an unknown entity.")
                    allowed = (
                        {"encountered", "friendly", "neutral", "hostile", "captive", "dead", "defeated", "released"}
                        if entity.kind == "actor" else {"discovered"}
                    )
                    if not set(required.statuses) <= allowed:
                        raise ValueError("Resolution prerequisite has an unsupported entity status.")
                else:
                    definition = definitions[required.kind].get(required.target_id)
                    if definition is None or not set(required.statuses) <= set(definition.statuses):
                        raise ValueError("Resolution prerequisite names an unknown definition or status.")
            for effect in _policy_effects(policy):
                if not set(effect.passage_ids) <= passages:
                    raise ValueError("Resolution effect names an unknown passage.")
                if not any(
                    passage.audience == "player"
                    and set(passage.scene_ids).intersection(policy.scene_ids)
                    for passage in self.passages
                    if passage.passage_id in effect.passage_ids
                ):
                    raise ValueError("Player consequences need reviewed public passage evidence.")
                target_kind = _POLICY_TARGET_KIND[effect.kind]
                if target_kind in definitions:
                    definition = definitions[target_kind].get(effect.target_id)
                    if definition is None:
                        raise ValueError("Resolution effect names an unknown story definition.")
                    if not set(effect.passage_ids) & set(definition.passage_ids):
                        raise ValueError("Resolution effect evidence must support its definition.")
                    if effect.status not in definition.statuses:
                        raise ValueError("Resolution effect has an unsupported definition status.")
                    if effect.status == "announced" and not set(effect.passage_ids) <= set(
                        definition.announcement_passage_ids
                    ):
                        raise ValueError(
                            "Announcement effects need explicit reviewed announcement evidence."
                        )
                    if effect.kind == "clock" and effect.quantity_delta is not None:
                        if effect.status not in {"advanced", "completed"}:
                            raise ValueError("Clock effects must advance or complete a clock.")
                elif target_kind == "entity":
                    entity = entities_by_id.get(effect.target_id)
                    if entity is None:
                        raise ValueError("Resolution effect names an unknown entity.")
                    if effect.kind == "discover" and entity.kind not in {
                        "clue",
                        "hint",
                        "hazard",
                        "ending",
                    }:
                        raise ValueError(
                            "Only clues, hazards or endings can be policy discoveries."
                        )
                    if effect.kind in {"encounter", "relationship"} and entity.kind != "actor":
                        raise ValueError("Encounter and relationship effects need an actor entity.")
                    if not set(effect.passage_ids) & {
                        passage.passage_id
                        for passage in self.passages
                        if effect.target_id in passage.entity_ids
                    }:
                        raise ValueError("Resolution effect evidence must identify its entity.")
                elif target_kind == "scene":
                    if effect.kind == "learn_place":
                        if effect.target_id not in scenes or not all(
                            effect.target_id in p.known_scene_ids
                            for p in self.passages
                            if p.passage_id in effect.passage_ids
                        ):
                            raise ValueError(
                                "Known-place effects need explicit mentioned-place evidence."
                            )
                    elif effect.target_id not in scenes or effect.status != "visited":
                        raise ValueError("Move effects need a known scene with visited status.")
                else:
                    raise ValueError("Resolution effect kind has no reviewed definition category.")
                if effect.kind == "transfer" and effect.destination not in policy.scene_ids:
                    raise ValueError("Transfer effects must target the policy's current scene.")
                if effect.kind in {"acquire", "consume", "transfer", "use", "environment"}:
                    definition = definitions["object"].get(effect.target_id)
                    if definition is None:
                        raise ValueError("Resolution effect names an unknown object definition.")
                    if (
                        effect.kind in {"use", "environment"}
                        and effect.operation not in definition.operations
                    ):
                        raise ValueError(
                            "Object effect needs an operation reviewed for that object."
                        )
                    if effect.kind == "use" and effect.destination is not None:
                        destination = definitions["object"].get(effect.destination)
                        if destination is None or not set(destination.scene_ids).intersection(
                            policy.scene_ids
                        ):
                            raise ValueError(
                                "Use effects name an unknown or out-of-scene destination."
                            )
                if effect.kind == "discover":
                    entity = entities_by_id[effect.target_id]
                    if entity.kind not in {"clue", "hint", "hazard", "ending"}:
                        raise ValueError(
                            "Only clues, hazards or endings can be policy discoveries."
                        )
                    if not entity.discovery_flag or not any(
                        passage.audience == "player"
                        and effect.target_id in passage.entity_ids
                        and passage.passage_id in effect.passage_ids
                        for passage in self.passages
                    ):
                        raise ValueError("Policy discoveries need exact public entity passages.")
                    if not any(
                        passage.audience == "player"
                        and effect.target_id in passage.entity_ids
                        and passage.passage_id in policy.passage_ids
                        for passage in self.passages
                    ):
                        raise ValueError(
                            "Policy discoveries need retrieved public evidence for the entity."
                        )
                    if effect in policy.on_failure and entity.reveal_on != "always":
                        raise ValueError(
                            "Failure discoveries must be explicitly reviewed for failure reveal."
                        )
            if policy.mode == "automatic" and not policy.on_automatic:
                raise ValueError("Automatic policies must declare their durable result.")
        for clock in self.clocks:
            if clock.clock_max is None:
                raise ValueError("Reviewed clocks need an explicit maximum.")
        self._validate_opening_journal()
        for entity in self.entities:
            if entity.object_id is not None and (
                entity.kind not in {"clue", "hint"}
                or entity.object_id not in {d.canonical_id for d in self.objects}
            ):
                raise ValueError("Clue object binding needs a reviewed object in this manifest.")
            reference = entity.stat_reference
            lair = entity.lair_reference
            if entity.surprise is not None and entity.kind != "actor":
                raise ValueError("Only an actor can declare encounter surprise.")
            if lair is not None and (entity.kind != "actor" or reference is None):
                raise ValueError("Lair actions require a reviewed actor statistics reference.")
            if reference is None:
                continue
            if entity.kind != "actor":
                raise ValueError("Only actors can reference creature statistics.")
            if reference.kind == "template" and reference.rules_edition != self.rules_edition:
                raise ValueError("Actor template edition must match its adventure manifest.")
            if reference.kind == "module":
                passage = next(
                    (p for p in self.passages if p.passage_id == reference.passage_id), None
                )
                if (
                    passage is None
                    or entity.entity_id not in passage.entity_ids
                    or passage.audience not in ("player", "planner")
                ):
                    raise ValueError(
                        "Module statistics must be a playable reviewed passage for this actor."
                    )
            if lair is not None:
                passage = next((p for p in self.passages if p.passage_id == lair.passage_id), None)
                if (
                    self.rules_edition not in {"2014", "2024"}
                    or passage is None
                    or entity.entity_id not in passage.entity_ids
                    or passage.audience != "planner"
                    or lair.creature_name.strip().casefold()
                    != reference.creature_name.strip().casefold()
                ):
                    raise ValueError(
                        "Lair actions require an exact planner passage bound to the reviewed actor and creature."
                    )
        return self

    def _validate_opening_journal(self) -> None:
        passages = {p.passage_id: p for p in self.passages}
        seen = set()
        for seed in self.opening_journal:
            key = (seed.kind, seed.target_id)
            if key in seen:
                raise ValueError("Opening journal cannot repeat a fact.")
            seen.add(key)
            if any(pid not in passages for pid in seed.passage_ids):
                raise ValueError("Opening seed names an unknown passage.")
            evidence = [passages[pid] for pid in seed.passage_ids]
            if any(
                p.audience != "player" or p.requires or self.opening_scene_id not in p.scene_ids
                for p in evidence
            ):
                raise ValueError(
                    "Opening seeds require unconditional player passages in the opening."
                )
            if seed.kind in {"discover", "encounter"}:
                entity = next((e for e in self.entities if e.entity_id == seed.target_id), None)
                if entity is None or not all(seed.target_id in p.entity_ids for p in evidence):
                    raise ValueError("Opening entity seed needs its own exact entity evidence.")
                if seed.kind == "encounter":
                    valid = entity.kind == "actor" and seed.status == "encountered"
                else:
                    valid = (
                        entity.kind in {"clue", "hint", "hazard", "ending"}
                        and seed.status == "discovered"
                    )
            elif seed.kind in {"visit", "learn_place"}:
                valid = (
                    seed.kind == "visit"
                    and seed.target_id == self.opening_scene_id
                    and seed.status == "visited"
                ) or (
                    seed.kind == "learn_place"
                    and seed.status == "known"
                    and all(seed.target_id in p.known_scene_ids for p in evidence)
                )
            else:
                definitions = self.objectives if seed.kind == "objective" else self.milestones
                definition = next(
                    (d for d in definitions if d.canonical_id == seed.target_id), None
                )
                valid = bool(
                    definition
                    and not definition.requires
                    and self.opening_scene_id in definition.scene_ids
                    and set(seed.passage_ids) <= set(definition.passage_ids)
                    and seed.status in definition.statuses
                )
                if seed.status == "announced":
                    valid = valid and set(seed.passage_ids) <= set(
                        definition.announcement_passage_ids if definition else []
                    )
            if not valid:
                raise ValueError(
                    "Opening journal seed has an unsupported target, state or evidence."
                )


def manifest_digest(manifest: AdventureManifest) -> str:
    """Receipt covers all structure/classification; editing a published draft revokes it."""
    body = manifest.model_dump(exclude={"reviewed_sha256", "reviewed_by"})
    # Absent optional actor bindings must not revoke receipts issued before this extension.
    # Adding or changing an explicit binding still changes the complete review digest.
    for entity in body["entities"]:
        if entity["reveal_on"] == "success":
            entity.pop("reveal_on")
        if entity["stat_reference"] is None:
            entity.pop("stat_reference")
        if entity["object_id"] is None:
            entity.pop("object_id")
        if entity.get("lair_reference") is None:
            entity.pop("lair_reference")
        if entity.get("surprise") is None:
            entity.pop("surprise")
    for passage in body["passages"]:
        if not passage["known_scene_ids"]:
            passage.pop("known_scene_ids")
    for policy in body["resolution_policies"]:
        if not policy["requires_facts"]:
            policy.pop("requires_facts")
    if body["prepared"] is not None:
        if body["prepared"]["narrative_mode"] == "scripted":
            body["prepared"].pop("narrative_mode")
        for quest in body["prepared"]["quests"]:
            if quest["required_for_completion"]:
                quest.pop("required_for_completion")
            if not quest["required_milestone_ids"]:
                quest.pop("required_milestone_ids")
    for category in ("objects", "objectives", "milestones", "promises", "clocks"):
        for definition in body[category]:
            for field in ("completion_condition", "engine_requirements", "engine_forbidden"):
                if not definition[field]:
                    definition.pop(field)
            if not definition["announcement_passage_ids"]:
                definition.pop("announcement_passage_ids")
            if definition["clock_reveal"] is None:
                definition.pop("clock_reveal")
    if not body["opening_journal"]:
        body.pop("opening_journal")
    for key in ("objects", "objectives", "milestones", "promises", "clocks"):
        if not body[key]:
            body.pop(key)
    if not body["resolution_policies"]:
        body.pop("resolution_policies")
    if body["prepared"] is None:
        body.pop("prepared")
    encoded = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return text_digest(encoded)


def require_published(
    value: dict | AdventureManifest, expected: dict | None = None
) -> AdventureManifest:
    try:
        manifest = AdventureManifest.model_validate(
            value.model_dump() if isinstance(value, AdventureManifest) else value
        )
    except (ValueError, TypeError) as exc:
        raise ManifestError("Adventure manifest is invalid. Compile and review it again.") from exc
    if (
        not manifest.reviewed_by
        or manifest.reviewed_sha256 != manifest_digest(manifest)
        or any(p.audience == "unreviewed" or not p.text_sha256 for p in manifest.passages)
    ):
        raise ManifestError("Adventure manifest is unreviewed or changed after review.")
    if manifest.prepared is not None:
        from edgar_core.prepared import validate_prepared_campaign

        report = validate_prepared_campaign(manifest, manifest.prepared)
        if not report.valid:
            raise ManifestError(
                "Prepared campaign failed deterministic validation. Inspect the private preparation and retry."
            )
    if not manifest.opening_scene_id or not any(
        p.audience == "player" and manifest.opening_scene_id in p.scene_ids and not p.requires
        for p in manifest.passages
    ):
        raise ManifestError("Choose an opening scene with unconditional reviewed player text.")
    for field in ("source_sha256", "extraction_sha256", "rules_edition"):
        if (
            expected
            and expected.get(field) is not None
            and getattr(manifest, field) != expected[field]
        ):
            raise ManifestError(
                f"Adventure manifest has a stale {field}; review the current source."
            )
    return manifest


def load_published_manifest(metadata: dict) -> AdventureManifest:
    """Existing adventure JSON is the authority; legacy sources remain explicitly unreviewed."""
    if not metadata.get("manifest"):
        raise ManifestError("This adventure needs a reviewed scene manifest before module play.")
    expected = dict(metadata.get("ingestion") or {})
    if metadata.get("rules_edition") is not None:
        expected["rules_edition"] = metadata["rules_edition"]
    if not expected.get("source_sha256") or not expected.get("extraction_sha256"):
        raise ManifestError("This adventure has no validated extraction identity for its manifest.")
    return require_published(metadata["manifest"], expected)


def select_scene_context(
    value: dict | AdventureManifest,
    current_scene_id: str | None,
    world_flags: dict[str, str] | None = None,
) -> dict:
    """Current scene + explicit cross references, gated by already persisted prerequisites.

    Links expose eligible canonical destinations to the planner without loading their text.
    No transitive traversal or semantic search can pull in an unrelated future scene.
    """
    manifest = require_published(value)
    scene = next((s for s in manifest.scenes if s.scene_id == current_scene_id), None)
    if scene is None:
        raise ManifestError("Current scene is absent or unknown; select a reviewed opening scene.")
    flags = world_flags or {}

    def unlocked(requirements):
        return all(flags.get(key) == value for key, value in requirements.items())

    selected = [
        p
        for p in manifest.passages
        if (scene.scene_id in p.scene_ids or p.passage_id in scene.references)
        and unlocked(p.requires)
        and p.audience in ("player", "planner")
    ]
    entity_ids = {eid for p in selected for eid in p.entity_ids}
    return {
        "scene_id": scene.scene_id,
        "player_passage_ids": [p.passage_id for p in selected if p.audience == "player"],
        "planner_passage_ids": [p.passage_id for p in selected if p.audience == "planner"],
        "allowed_scene_ids": [scene.scene_id]
        + [link.scene_id for link in scene.links if unlocked(link.requires)],
        "entities": [e.model_dump() for e in manifest.entities if e.entity_id in entity_ids],
    }
