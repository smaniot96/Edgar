from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class CampaignCreate(BaseModel):
    """Request body for POST /campaigns."""
    title: str = Field(..., min_length=1, max_length=255)
    system: str = Field(..., min_length=1, max_length=255)
    created_by: int | None = None  # optional if set from auth
    adventure_collections: list[str] | None = None


class CampaignRead(BaseModel):
    """Response body — mirrors Campaign ORM columns."""
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    system: str
    created_by: int
    created_at: datetime
    adventure_collections: list[str]
    status: Literal["active", "ended"]
    ended_at: datetime | None = None


class CampaignUpdate(BaseModel):
    """Request body for PATCH /campaigns/{id} — all optional."""
    title: str | None = Field(None, min_length=1, max_length=255)
    system: str | None = Field(None, min_length=1, max_length=255)
    adventure_collections: list[str] | None = None
