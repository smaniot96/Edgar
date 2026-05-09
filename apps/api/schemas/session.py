from datetime import datetime
from typing import Literal

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
    current_scene_id: str | None = None
    active_character_id: int | None = None


class ChatMessageRead(BaseModel):
    """One line in the play UI message log (from event_log narration rows)."""

    role: Literal["user", "dm"]
    content: str
    created_at: datetime


class SessionUpdate(BaseModel):
    """Request body for PATCH /sessions/{id} — all optional."""
    started_at: datetime | None = None
    ended_at: datetime | None = None
    active_character_id: int | None = None
