from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from edgar_core.preparation_status import SafePreparationFailure
from edgar_core.sources import RulesEdition, SourceEdition

from .preparation_setup import PreparationSetup
from .readiness import ReadinessRead

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
    library_archived: bool = False
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
    readiness: ReadinessRead | None = None


class AdventureLibraryUpdate(BaseModel):
    archived: bool | None = None
    title: str | None = Field(default=None, min_length=1, max_length=255)

    @field_validator("title")
    @classmethod
    def clean_title(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("An adventure title cannot be blank.")
        return value.strip() if value is not None else value

    @model_validator(mode="after")
    def require_change(self):
        if self.archived is None and self.title is None:
            raise ValueError("Provide a title or archived preference.")
        return self


class AdventureUpdate(BaseModel):
    rules_edition: SourceEdition = "unspecified"
    title: str | None = None
    description: str | None = None
    level_range: str | None = None
    cover_image: str | None = None

    @field_validator("cover_image")
    @classmethod
    def validate_cover_image(cls, value: str | None) -> str | None:
        from ..services.cover_images import normalize_cover_image

        return normalize_cover_image(value)


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
    # Optional client identity (UUID/ULID). A retry with the same identity and body replays the
    # first accepted module; reusing it with a different body is rejected with 409.
    request_id: str | None = Field(
        default=None, min_length=26, max_length=36, pattern=r"^[0-9A-Za-z-]+$"
    )
