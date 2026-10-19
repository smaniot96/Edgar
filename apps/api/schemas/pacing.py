"""Table pacing and queued-action shapes (EDG-051).

Pacing is part of the spotlight read (``SpotlightRead.pacing``) so every member sees the same policy.
A queued action's text is returned only to its author; the table sees that it exists and its status.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

PacingPolicyName = Literal["relaxed", "standard", "fast"]
PacingPolicyState = Literal["relaxed", "standard", "fast", "custom"]
QueuedStatus = Literal["queued", "stale", "expired"]
StaleReason = Literal["scene_changed", "combat_changed", "hero_state_changed"]


class PacingRead(BaseModel):
    policy: PacingPolicyState
    label: str
    summary: str
    timeout_seconds: int
    # A holder who has left the table is passed early (fast policy).
    auto_pass: bool
    queue_ttl_seconds: int


class QueuedActionRead(BaseModel):
    id: str
    user_id: int
    display_name: str | None = None
    character_id: int
    character_name: str | None = None
    status: QueuedStatus
    stale_reason: StaleReason | None = None
    version: int
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    # Present only for the author.
    text: str | None = None


class QueueRead(BaseModel):
    revision: int
    items: list[QueuedActionRead] = Field(default_factory=list)


def _clean(value: str) -> str:
    text = value.strip()
    if not text:
        raise ValueError("The action cannot be empty.")
    return text


class QueuedActionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(..., min_length=1, max_length=2000)
    # A retry of the same submission returns the existing item instead of failing as a duplicate.
    request_id: str | None = Field(
        default=None, min_length=1, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$"
    )
    # The spotlight version the player saw; a mismatch is refused (409 spotlight_stale).
    spotlight_version: int | None = Field(default=None, ge=0)

    _text = field_validator("text")(_clean)


class QueuedActionEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(..., min_length=1, max_length=2000)
    expected_version: int = Field(..., ge=1)

    _text = field_validator("text")(_clean)
