"""Schemas for turn endpoint."""

import os

from pydantic import BaseModel, Field, field_validator


def debug_turns_enabled() -> bool:
    """Server-side gate for developer debug output (read per request so tests can toggle it)."""
    return os.environ.get("EDGAR_DEBUG", "").strip().lower() in {"1", "true", "yes"}


class TurnRequest(BaseModel):
    """Request body for POST /sessions/{session_id}/turn."""

    message: str = Field(..., min_length=1, max_length=2000)
    # Developer mode: when true, the stream also emits a `debug` SSE frame (intent, retrieved
    # RAG chunks, full combat internals incl. enemy stats, stage timings, model). Honoured only
    # when the server runs with EDGAR_DEBUG=1; otherwise silently forced off.
    debug: bool = False

    @field_validator("debug")
    @classmethod
    def _gate_debug(cls, value: bool) -> bool:
        return value and debug_turns_enabled()


class TurnResponse(BaseModel):
    """Response body for turn endpoint."""

    narration: str
    adjudication: dict | None = None
    combat_state: dict | None = None
    current_scene_id: str | None = None
    character: dict | None = None
    campaign_complete: bool = False
