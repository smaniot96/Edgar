from typing import Literal

from pydantic import BaseModel, Field

AdventureStatus = Literal[
    "processing", "ready", "failed", "missing", "empty", "partial", "unavailable"
]


class AdventureRead(BaseModel):
    slug: str
    title: str
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


class AdventureUpdate(BaseModel):
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


CampaignSize = Literal["small", "medium", "large", "gigantic"]


class AdventureGenerateRequest(BaseModel):
    """Ask the AI to author a full campaign. `size` scales how long/complex it is."""

    title: str | None = None
    theme: str | None = None  # free-text premise/setting/tone the player wants
    size: CampaignSize = "medium"
