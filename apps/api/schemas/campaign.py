from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class CampaignCreate(BaseModel):
    """Request body for POST /campaigns."""
    title: str = Field(..., min_length=1, max_length=255)
    system: str = Field(..., min_length=1, max_length=255)
    created_by: int | None = None  # optional if set from auth


class CampaignRead(BaseModel):
    """Response body — mirrors Campaign ORM columns."""
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    system: str
    created_by: int
    created_at: datetime


class CampaignUpdate(BaseModel):
    """Request body for PATCH /campaigns/{id} — all optional."""
    title: str | None = Field(None, min_length=1, max_length=255)
    system: str | None = Field(None, min_length=1, max_length=255)
