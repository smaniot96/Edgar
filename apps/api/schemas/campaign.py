from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ._validators import forbid_null


class CampaignCreate(BaseModel):
    """Request body for POST /campaigns. The owner is always the current user (any
    client-sent `created_by` is ignored)."""

    title: str = Field(..., min_length=1, max_length=255)
    system: str = Field(..., min_length=1, max_length=255)
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
    """Request body for PATCH /campaigns/{id} — all optional, none nullable."""

    title: str | None = Field(None, min_length=1, max_length=255)
    system: str | None = Field(None, min_length=1, max_length=255)
    adventure_collections: list[str] | None = None

    reject_nulls = forbid_null("title", "system", "adventure_collections")
