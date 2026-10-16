from typing import Literal

from pydantic import BaseModel, Field

from edgar_core.preparation import SafePreparationFailure
from edgar_core.sources import RulesEdition, SourceEdition

from .preparation_setup import PreparationSetup

AdventureStatus = Literal[
    "processing", "ready", "failed", "missing", "empty", "partial", "unavailable"
]


class AdventureJobRead(BaseModel):
    """Safe scalar job state; never includes outline, chapter, or manuscript content."""

    version: Literal[1] = 1
    job_id: str
    kind: Literal["generation", "ingestion"]
    state: Literal[
        "queued", "running", "interrupted", "failed", "completed", "cancel_requested", "cancelled"
    ]
    stage: str
    completed_chapters: int = 0
    total_chapters: int | None = None
    checkpoint_available: bool = False
    retryable: bool = False
    retained_work: Literal[
        "none",
        "outline_and_chapters",
        "complete_manuscript",
        "source_pdf",
        "inspected_extraction",
        "published",
    ] = "none"
    action_label: str | None = None
    explanation: str | None = None
    source_version: str | None = None
    failure: SafePreparationFailure | None = None


class AdventureRead(BaseModel):
    slug: str
    title: str
    rules_edition: SourceEdition = "unspecified"
    description: str | None = None
    level_range: str | None = None
    cover_image: str | None = None
    # Ingestion lifecycle: "processing" while embedding, "ready" when searchable, "failed" on error.
    status: AdventureStatus = "ready"
    usable_chunks: int = 0
    source_pdf_available: bool = False
    retrieval_degraded: bool = False
    chunks: int | None = None
    source_filename: str | None = None
    error: str | None = None
    created_at: str | None = None
    failure_code: str | None = None
    extraction_preview_available: bool = False
    extraction_preview_current: bool = False
    ingestion: dict | None = None
    can_retry: bool = False
    job: AdventureJobRead | None = None
    preparation_setup_required: bool = False


class AdventureUpdate(BaseModel):
    rules_edition: SourceEdition = "unspecified"
    title: str | None = None
    description: str | None = None
    level_range: str | None = None
    cover_image: str | None = None


class AdventureUploadResponse(BaseModel):
    slug: str
    title: str
    status: AdventureStatus


class AdventureRetryRequest(BaseModel):
    reviewed_source_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    reviewed_extraction_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class AdventurePreparationRequest(BaseModel):
    rules_edition: RulesEdition
    preparation: PreparationSetup


CampaignSize = Literal["small", "medium", "large", "gigantic"]


class AdventureGenerateRequest(BaseModel):
    """Ask the AI to author a full campaign. `size` scales how long/complex it is."""

    title: str | None = None
    theme: str | None = None  # free-text premise/setting/tone the player wants
    size: CampaignSize = "medium"
    rules_edition: RulesEdition = "2014"
    preparation: PreparationSetup | None = None
