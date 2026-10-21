"""Campaign preparation v2: reusable narrative material, never committed play state."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from edgar_core.identifiers import Identifier

SCHEMA_VERSION = "campaign-semantic-2.0"


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Origin(Record):
    kind: Literal["pdf", "authored", "adaptation", "invention"]
    pages: list[int] = Field(default_factory=list)
    regions: list[str] = Field(default_factory=list)
    note: str = ""
    uncertain: bool = False


class Entity(Record):
    id: Identifier
    kind: Literal["npc", "place", "faction", "clue"]
    name: str
    aliases: list[str] = Field(default_factory=list)
    description: str
    motive: str = ""
    knowledge: str = ""
    origin: Origin


class Secret(Record):
    id: Identifier
    truth: str
    known_by: list[Identifier] = Field(default_factory=list)
    setup_chapters: list[Identifier] = Field(default_factory=list)
    reveal_chapters: list[Identifier] = Field(default_factory=list)
    reveal_condition: str
    origin: Origin


class Chapter(Record):
    id: Identifier
    title: str
    brief: str
    source_order: int
    play_order: int
    chronology: str = "relative/unknown"
    pages: list[int] = Field(default_factory=list)
    entity_ids: list[Identifier] = Field(default_factory=list)
    secret_ids: list[Identifier] = Field(default_factory=list)
    depends_on: list[Identifier] = Field(default_factory=list)


class Spine(Record):
    title: str
    premise: str
    setting: str
    hook: str
    ending: str
    level_recommendation: str = ""
    dm_guidance: str
    chapters: list[Chapter] = Field(min_length=1)
    entities: list[Entity] = Field(default_factory=list)
    secrets: list[Secret] = Field(default_factory=list)
    important_visual_pages: list[int] = Field(default_factory=list)


class Goal(Record):
    id: Identifier
    title: str
    objective: str
    completion: str = Field(
        description="Outcome a live DM can recognize through any credible creative solution."
    )
    required: bool = True
    event_ids: list[Identifier] = Field(min_length=1)
    depends_on: list[Identifier] = Field(default_factory=list)
    origin: Origin


class Encounter(Record):
    actor_id: Identifier
    creature: str = Field(description="Exact verified template name; no invented numbers.")
    quantity: int = Field(ge=1)
    adaptation: str = Field(
        default="", description="Explain any deliberate replacement of source mechanics."
    )


class Reveal(Record):
    entity_id: Identifier
    public_text: str
    condition: str = Field(description="Private discovery guidance; allow any credible approach.")


class Event(Record):
    id: Identifier
    title: str
    location_id: Identifier
    participants: list[Identifier] = Field(default_factory=list)
    goal_ids: list[Identifier] = Field(default_factory=list)
    secret_ids: list[Identifier] = Field(default_factory=list)
    clue_ids: list[Identifier] = Field(default_factory=list)
    visible_entity_ids: list[Identifier] = Field(default_factory=list)
    reveals: list[Reveal] = Field(default_factory=list)
    trigger: str
    timing: str
    public_description: str = Field(
        description="Player-safe sensory situation before actions; no hidden truths."
    )
    dm_notes: str = Field(
        description="Useful opportunities, motives, clues, optional paths and consequences, not committed events."
    )
    next_event_ids: list[Identifier] = Field(default_factory=list)
    encounters: list[Encounter] = Field(default_factory=list)
    origin: Origin


class Bundle(Record):
    chapter_id: Identifier
    goals: list[Goal] = Field(min_length=1)
    events: list[Event] = Field(min_length=1)
    new_entities: list[Entity] = Field(default_factory=list)
    new_secrets: list[Secret] = Field(default_factory=list)
    dm_guidance: str


class Bundles(Record):
    chapters: list[Bundle] = Field(min_length=1)


class Finding(Record):
    id: Identifier
    severity: Literal["material", "advice"]
    record_ids: list[Identifier] = Field(min_length=1)
    evidence: str
    instruction: str


class Review(Record):
    covered_ids: list[Identifier]
    findings: list[Finding] = Field(default_factory=list)


class Patch(Record):
    """Typed record patches; only IDs in the dependency closure may change."""

    spine: Spine | None = None
    entities: list[Entity] = Field(default_factory=list)
    secrets: list[Secret] = Field(default_factory=list)
    goals: list[Goal] = Field(default_factory=list)
    events: list[Event] = Field(default_factory=list)


class IdentityJoin(Record):
    canonical_id: Identifier
    duplicate_ids: list[Identifier] = Field(min_length=1)
    reason: str


class ContinuityPatch(Patch):
    joins: list[IdentityJoin] = Field(default_factory=list)


class SourceReading(Record):
    summary: str
    chapter_candidates: list[Chapter] = Field(default_factory=list)
    important_visual_pages: list[int] = Field(default_factory=list)


class VisualEvidence(Record):
    page: int
    interpretation: str
    uncertainties: list[str]
    mechanical_text_verified: Literal[False] = False
