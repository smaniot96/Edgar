"""Schemas for turn endpoint."""

import os

from pydantic import BaseModel, ConfigDict, Field, field_validator


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
    # EDG-048: the spotlight version the client acted on. A mismatch is refused (409) before any
    # model call. Optional, so clients that do not track the table keep working.
    spotlight_version: int | None = Field(default=None, ge=0)
    # EDG-051: play the caller's queued action. `message` must equal its text and `queued_action_version`
    # (optional) its version, or the turn is refused before any model call. The server validates it
    # again against the current situation and consumes it in the turn's own commit.
    queued_action_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    queued_action_version: int | None = Field(default=None, ge=1)
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

    event_id: int | None = None
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


class CombatSkipRequest(BaseModel):
    """Skip the idle hero's turn in a shared fight (EDG-049). The version is the one the client last saw."""

    model_config = ConfigDict(extra="forbid")

    spotlight_version: int | None = Field(default=None, ge=0)
