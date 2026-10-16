"""Safe, additive projection of the gates which control starting a campaign turn."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ReadinessState = Literal[
    "ready",
    "source_missing",
    "source_unavailable",
    "source_review_needed",
    "adventure_review_needed",
    "saved_version_conflict",
    "preparation_in_progress",
    "preparation_failed",
    "rules_edition_mismatch",
    "choose_character",
    "character_assignment_conflict",
    "completed",
    "readiness_unavailable",
]

ReadinessAction = Literal[
    "play",
    "read_history",
    "recover_source",
    "review_source",
    "review_adventure",
    "wait_for_preparation",
    "repair_preparation",
    "choose_character",
    "reopen_campaign",
    "retry_readiness",
]


class ReadinessBlocker(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: ReadinessState
    message: str
    action: ReadinessAction


class ReadinessRead(BaseModel):
    """Player-safe current readiness; never a permission token for a future turn."""

    model_config = ConfigDict(extra="forbid")

    play_allowed: bool
    history_allowed: bool = True
    state: ReadinessState
    primary_action: ReadinessAction
    blockers: list[ReadinessBlocker] = Field(default_factory=list)
