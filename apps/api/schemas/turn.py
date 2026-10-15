"""Schemas for turn endpoint."""

import os

from pydantic import BaseModel, Field, field_validator


def debug_turns_enabled() -> bool:
    """Server-side gate for developer debug output (read per request so tests can toggle it)."""
    return os.environ.get("EDGAR_DEBUG", "").strip().lower() in {"1", "true", "yes"}


class TurnRequest(BaseModel):
    """Request body for POST /sessions/{session_id}/turn."""

    message: str = Field(..., min_length=1, max_length=2000)
    # Optional for legacy clients. A retry keeps this identity so a lost acknowledgment
    # cannot spend a second model turn or apply the committed action twice.
    request_id: str | None = Field(
        default=None, min_length=1, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$"
    )
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

    retrieval_degraded: bool = False
    source_status: list[dict] = Field(default_factory=list)
    source_mode: str = "module"
    narration: str
    citations: list[dict] = Field(default_factory=list)
    rules_edition: str = "2014"
    ruling_citation_ids: list[str] = Field(default_factory=list)
    adjudication: dict | None = None
    combat_state: dict | None = None
    current_scene_id: str | None = None
    character: dict | None = None
    campaign_complete: bool = False
    # Developer-only (debug requested and EDGAR_DEBUG=1); null otherwise. EDG-031.
    combat_diagnostics: dict | None = None
    # The turn's original roll (EDG-030), the same record the reload path returns. Null when no dice.
    roll_status: str | None = None
    roll: dict | None = None
