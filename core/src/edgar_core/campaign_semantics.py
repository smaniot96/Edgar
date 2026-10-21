"""Campaign preparation v2: reusable narrative material, never committed play state."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from edgar_core.identifiers import Identifier

SCHEMA_VERSION = "campaign-semantic-2.1"


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


class MechanicalProfile(Record):
    id: Identifier
    creature: str = Field(
        description="Exact verified catalogue name or original bespoke profile name."
    )
    source_stat_block: str = Field(
        default="",
        description="One complete original bespoke/modified profile; empty for a verified catalogue template. Define once and reuse its ID.",
    )
    source_pages: list[int] = Field(default_factory=list)


class Encounter(Record):
    actor_id: Identifier
    profile_id: Identifier | None = Field(
        default=None,
        description="Reference to one shared mechanical profile. Repeated appearances and disguises of the same individual reuse its profile.",
    )
    creature: str = Field(
        default="",
        description="Original source creature name when source_stat_block is supplied; otherwise an exact verified template name.",
    )
    quantity: int = Field(ge=1)
    adaptation: str = Field(
        default="", description="Explain any deliberate replacement of source mechanics."
    )
    source_stat_block: str = Field(
        default="",
        description="Complete source profile, including source-specific attacks, spells and traits. Correct OCR layout from supplied image evidence; never invent statistics or replace a bespoke profile merely to fit the catalogue.",
    )
    source_pages: list[int] = Field(default_factory=list)


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


class SourceItem(Record):
    entity_id: Identifier
    procedure: str = Field(
        description="Complete original operating rules, resource limits, eligibility, checks, failure tiers and consequences; retain exact numbers and distinguish engine limitations."
    )
    event_ids: list[Identifier] = Field(min_length=1)
    portable: bool = True
    consumable: bool = False
    origin: Origin


class SourceCheck(Record):
    id: Identifier
    action: str
    event_ids: list[Identifier] = Field(min_length=1)
    check: str = Field(
        description="Engine skill/ability/tool identifier, for example arcana, persuasion, athletics or thieves_tools."
    )
    dc: int = Field(ge=1, le=50)
    success_text: str
    failure_text: str
    origin: Origin


class Bundle(Record):
    chapter_id: Identifier
    mechanical_profiles: list[MechanicalProfile] = Field(default_factory=list)
    goals: list[Goal] = Field(min_length=1)
    events: list[Event] = Field(min_length=1)
    new_entities: list[Entity] = Field(default_factory=list)
    new_secrets: list[Secret] = Field(default_factory=list)
    dm_guidance: str
    source_pages: list[int] = Field(default_factory=list)
    source_notes: str = Field(
        default="",
        description="Detailed chapter running reference: original tactics, conditional timing, spatial facts, alternatives, roleplay and humor, rewards, consequences and applicable appendix procedures. Useful detail, not a synopsis. Source OCR is also retained separately.",
    )
    items: list[SourceItem] = Field(default_factory=list)
    checks: list[SourceCheck] = Field(default_factory=list)


class Bundles(Record):
    chapters: list[Bundle] = Field(min_length=1)
    mechanical_profiles: list[MechanicalProfile] = Field(default_factory=list)


class Finding(Record):
    id: Identifier
    severity: Literal["material", "advice"]
    record_ids: list[Identifier] = Field(min_length=1)
    evidence: str
    instruction: str


class Review(Record):
    covered_ids: list[Identifier]
    findings: list[Finding] = Field(default_factory=list)


class FieldEdit(Record):
    record_id: Identifier
    field: Literal["source_notes", "dm_notes"]
    old_text: str = Field(min_length=1, description="Exact unique substring of the accepted field.")
    new_text: str = Field(description="Replacement for only that substring; empty removes it.")


class Patch(Record):
    """Typed record patches; only IDs in the dependency closure may change."""

    spine: Spine | None = None
    entities: list[Entity] = Field(default_factory=list)
    secrets: list[Secret] = Field(default_factory=list)
    goals: list[Goal] = Field(default_factory=list)
    events: list[Event] = Field(default_factory=list)
    mechanical_profiles: list[MechanicalProfile] = Field(default_factory=list)
    field_edits: list[FieldEdit] = Field(
        default_factory=list,
        description="Sparse exact-text corrections to an allowed chapter source_notes or event dm_notes field. Preserve all surrounding text; do not restate that field in a record replacement.",
    )
    chapters: list[Bundle] = Field(
        default_factory=list,
        description="Full replacement of an affected chapter bundle only when its supplied chapter content record ID is in allowed_ids; preserve every unaffected contained record. Use individual patches for smaller changes.",
    )


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
