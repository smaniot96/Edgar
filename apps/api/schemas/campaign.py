from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ._validators import forbid_null

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


class CampaignCreate(BaseModel):
    """Request body for POST /campaigns. The owner is always the current user (any
    client-sent `created_by` is ignored)."""

    title: str = Field(..., min_length=1, max_length=255)
    system: str = Field(..., min_length=1, max_length=255)
    adventure_collections: list[str] | None = None
    source_mode: Literal["module", "homebrew"] = "module"


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
    sources: list[SourceRead] = Field(default_factory=list)
    retrieval_degraded: bool = False
    play_allowed: bool = True
    status: Literal["active", "ended"]
    ended_at: datetime | None = None


class CampaignUpdate(BaseModel):
    """Request body for PATCH /campaigns/{id} — all optional, none nullable."""

    title: str | None = Field(None, min_length=1, max_length=255)
    system: str | None = Field(None, min_length=1, max_length=255)
    adventure_collections: list[str] | None = None
    source_mode: Literal["module", "homebrew"] = "module"

    reject_nulls = forbid_null("title", "system", "adventure_collections", "source_mode")
