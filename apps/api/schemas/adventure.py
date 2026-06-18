from typing import Literal

from pydantic import BaseModel

AdventureStatus = Literal["processing", "ready", "failed"]


class AdventureRead(BaseModel):
    slug: str
    title: str
    description: str | None = None
    level_range: str | None = None
    cover_image: str | None = None
    # Ingestion lifecycle: "processing" while embedding, "ready" when searchable, "failed" on error.
    status: AdventureStatus = "ready"
    chunks: int | None = None
    source_filename: str | None = None
    error: str | None = None
    created_at: str | None = None


class AdventureUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    level_range: str | None = None
    cover_image: str | None = None


class AdventureUploadResponse(BaseModel):
    slug: str
    title: str
    status: AdventureStatus


CampaignSize = Literal["small", "medium", "large", "gigantic"]


class AdventureGenerateRequest(BaseModel):
    """Ask the AI to author a full campaign. `size` scales how long/complex it is."""

    title: str | None = None
    theme: str | None = None  # free-text premise/setting/tone the player wants
    size: CampaignSize = "medium"
