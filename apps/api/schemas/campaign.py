from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from agent.progression import DEFAULT_DEATH_MODE, DEFAULT_PROGRESSION_MODE
from edgar_core.sources import DEFAULT_RULES_EDITION, RulesEdition

from ._validators import forbid_null
from .adventure import AdventureJobRead
from .readiness import ReadinessRead

ProgressionMode = Literal["fixed", "milestone"]
DeathMode = Literal["subdued", "death_saves"]

SourceStatus = Literal[
    "ready", "missing", "empty", "partial", "unavailable", "processing", "failed"
]


class SourceRead(BaseModel):
    slug: str
    title: str
    status: SourceStatus
    usable_chunks: int = 0
    expected_chunks: int | None = None
    collection_exists: bool = False
    source_pdf_available: bool = False
    message: str | None = None
    failure_code: str | None = None
    extraction_preview_available: bool = False
    extraction_preview_current: bool = False
    ingestion: dict | None = None
    job: AdventureJobRead | None = None


SourceReleaseOutcome = Literal[
    "removed",  # no campaign used it any more, so the source was deleted
    "missing",  # it was already gone
    "in_use",  # another campaign still binds it
    "built_in",  # a bundled module; never removed with a campaign
    "protected",  # not an adventure source binding
    "not_permitted",  # only an admin may delete shared sources
    "processing",  # preparation is still running
    "retained",  # a prepared version kept for saved games (require_mutable_source)
    "unavailable",  # the vector store failed; nothing was removed
]


class SourceRelease(BaseModel):
    slug: str
    outcome: SourceReleaseOutcome


class CampaignDeleteRead(BaseModel):
    """What `DELETE /campaigns/{id}?remove_unused_sources=true` removed besides the campaign."""

    campaign_id: int
    sources: list[SourceRelease] = Field(default_factory=list)


class AdventureCampaignOpen(BaseModel):
    """Explicit new playthrough; omitted body retains legacy continue behavior."""

    new_campaign: bool = False
    request_id: UUID | None = None


class CampaignCreate(BaseModel):
    """Request body for POST /campaigns. The owner is always the current user (any
    client-sent `created_by` is ignored)."""

    title: str = Field(..., min_length=1, max_length=255)
    system: str = Field(..., min_length=1, max_length=255)
    adventure_collections: list[str] | None = None
    source_mode: Literal["module", "homebrew"] = "module"
    rules_edition: RulesEdition = DEFAULT_RULES_EDITION
    # EDG-045: explicit modes. The defaults keep the behaviour every campaign had before the modes.
    progression_mode: ProgressionMode = DEFAULT_PROGRESSION_MODE
    death_mode: DeathMode = DEFAULT_DEATH_MODE
    combat_always_confirm: bool = False


class CampaignRead(BaseModel):
    """Response body — mirrors Campaign ORM columns."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    system: str
    created_by: int
    created_at: datetime
    adventure_collections: list[str]
    source_mode: Literal["module", "homebrew"] = "module"
    rules_edition: RulesEdition = DEFAULT_RULES_EDITION
    progression_mode: ProgressionMode = DEFAULT_PROGRESSION_MODE
    death_mode: DeathMode = DEFAULT_DEATH_MODE
    combat_always_confirm: bool = False
    sources: list[SourceRead] = Field(default_factory=list)
    retrieval_degraded: bool = False
    play_allowed: bool = True
    readiness: ReadinessRead | None = None
    status: Literal["active", "ended"]
    ended_at: datetime | None = None


class CampaignUpdate(BaseModel):
    """Request body for PATCH /campaigns/{id} — all optional, none nullable."""

    title: str | None = Field(None, min_length=1, max_length=255)
    system: str | None = Field(None, min_length=1, max_length=255)
    adventure_collections: list[str] | None = None
    source_mode: Literal["module", "homebrew"] = "module"
    rules_edition: RulesEdition = DEFAULT_RULES_EDITION
    progression_mode: ProgressionMode = DEFAULT_PROGRESSION_MODE
    death_mode: DeathMode = DEFAULT_DEATH_MODE
    combat_always_confirm: bool = False

    reject_nulls = forbid_null(
        "title",
        "system",
        "adventure_collections",
        "source_mode",
        "rules_edition",
        "progression_mode",
        "death_mode",
        "combat_always_confirm",
    )
