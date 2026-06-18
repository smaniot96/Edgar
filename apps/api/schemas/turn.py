"""Schemas for turn endpoint."""

from pydantic import BaseModel, Field


class TurnRequest(BaseModel):
    """Request body for POST /sessions/{session_id}/turn."""

    message: str = Field(..., min_length=1, max_length=2000)
    # Developer mode: when true, the stream also emits a `debug` SSE frame (intent, retrieved
    # RAG chunks, full combat internals, stage timings, model).
    debug: bool = False


class TurnResponse(BaseModel):
    """Response body for turn endpoint."""

    narration: str
    adjudication: dict | None = None
    combat_state: dict | None = None
    current_scene_id: str | None = None
    character: dict | None = None
    campaign_complete: bool = False
