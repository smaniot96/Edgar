"""Schemas for turn endpoint."""

from pydantic import BaseModel, Field


class TurnRequest(BaseModel):
    """Request body for POST /sessions/{session_id}/turn."""

    message: str = Field(..., min_length=1)


class TurnResponse(BaseModel):
    """Response body for turn endpoint."""

    narration: str
    adjudication: dict | None = None
    combat_state: dict | None = None
