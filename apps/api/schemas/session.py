from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class SessionCreate(BaseModel):
    """Request body for POST /sessions."""
    campaign_id: int = Field(..., ge=1)
    started_at: datetime | None = None  # DB has server_default if omitted
    ended_at: datetime | None = None


class SessionRead(BaseModel):
    """Response body for session endpoints."""
    model_config = ConfigDict(from_attributes=True)

    id: int
    campaign_id: int
    started_at: datetime
    ended_at: datetime | None


class SessionUpdate(BaseModel):
    """Request body for PATCH /sessions/{id} — all optional."""
    started_at: datetime | None = None
    ended_at: datetime | None = None
