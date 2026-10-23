"""Small public status DTOs shared by current preparation and inert archive readers."""

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

PreparationRecord = BaseModel
RunId = str


class PreparationJobStatus(StrEnum):
    QUEUED = "queued"
    PREPARING = "preparing"
    REVIEWING = "reviewing"
    REPAIRING = "repairing"
    PUBLISHING = "publishing"
    READY = "ready"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"


class SafePreparationFailure(PreparationRecord):
    """Fixed taxonomy projection safe to expose in a preparation job response."""

    code: str
    category: Literal[
        "source",
        "review",
        "validation",
        "assignment",
        "busy",
        "provider_unavailable",
        "rate_limited",
        "quota_exceeded",
        "provider_configuration",
        "cancelled",
        "interrupted",
        "internal",
        "not_found",
    ]
    message: str
    recovery_action: Literal[
        "repair_source",
        "review_source",
        "fix_input",
        "assign_character",
        "wait",
        "retry",
        "check_existing_result",
        "configure_provider",
        "restart_preparation",
        "contact_support",
        "read_history",
    ]
    retryable: bool
    support_ref: str = Field(pattern=r"^err-[0-9a-f]{16}$")
    outcome: Literal["not_committed", "committed", "unknown"]


class PublicPreparationStatus(PreparationRecord):
    """Allowlisted player status; contains no titles, names, counts, findings or errors."""

    run_id: RunId
    status: PreparationJobStatus
    stage: str
    retryable: bool
    action: Literal["wait", "retry", "restart", "none"]
    failure: SafePreparationFailure | None = None
    retained_work: Literal[
        "none",
        "outline_and_chapters",
        "complete_manuscript",
        "source_pdf",
        "inspected_extraction",
        "published",
    ] = "none"
