"""Stable, player-safe failure categories shared by the API and agent.

Provider exception messages and response bodies can contain prompts, credentials, or source
content. This module classifies only exception types, numeric status codes, and fixed internal
codes. It never copies free-form exception text into a player response or log summary.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class ErrorCategory(StrEnum):
    SOURCE = "source"
    REVIEW = "review"
    VALIDATION = "validation"
    ASSIGNMENT = "assignment"
    BUSY = "busy"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    RATE_LIMITED = "rate_limited"
    QUOTA_EXCEEDED = "quota_exceeded"
    PROVIDER_CONFIGURATION = "provider_configuration"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"
    INTERNAL = "internal"
    NOT_FOUND = "not_found"


class RecoveryAction(StrEnum):
    REPAIR_SOURCE = "repair_source"
    REVIEW_SOURCE = "review_source"
    FIX_INPUT = "fix_input"
    ASSIGN_CHARACTER = "assign_character"
    WAIT = "wait"
    RETRY = "retry"
    CHECK_EXISTING_RESULT = "check_existing_result"
    CONFIGURE_PROVIDER = "configure_provider"
    RESTART_PREPARATION = "restart_preparation"
    CONTACT_SUPPORT = "contact_support"
    READ_HISTORY = "read_history"


class OutcomeState(StrEnum):
    NOT_COMMITTED = "not_committed"
    COMMITTED = "committed"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class FailureSpec:
    category: ErrorCategory
    code: str
    message: str
    status_code: int
    repair_action: RecoveryAction
    retryable_when_uncommitted: bool = False


_SPECS: dict[str, FailureSpec] = {}


def _register(spec: FailureSpec, *codes: str) -> None:
    for code in (spec.code, *codes):
        _SPECS[code] = spec


_register(
    FailureSpec(ErrorCategory.SOURCE, "campaign_sources_unavailable", "The adventure source needs attention before play can continue.", 409, RecoveryAction.REPAIR_SOURCE),
    "source_unavailable", "campaign_source_unavailable", "preparation_source_invalid",
    "source_missing", "extraction_quality", "index_coverage", "checkpoint_unreadable",
    "required_books_unavailable", "required_book_unavailable", "required_books_missing",
    "book_version_unavailable", "required_assets_unavailable",
)
_register(
    FailureSpec(ErrorCategory.REVIEW, "adventure_manifest_review_required", "Review the current adventure material before continuing.", 409, RecoveryAction.REVIEW_SOURCE),
    "manifest_review_required", "review_required", "source_review_required",
    "source_review_needed", "adventure_review_needed",
    "saved_version_conflict",
)
_register(
    FailureSpec(ErrorCategory.VALIDATION, "validation_failed", "Check the highlighted information and correct it before continuing.", 422, RecoveryAction.FIX_INPUT),
    "validation_error", "invalid_request", "preparation_setup_invalid", "stage_validation_failed",
    "unsupported_edition", "unsupported_party", "unsupported_progression", "unsupported_hero_capability",
    "rules_edition_mismatch",
    "preparation_setup_required", "task_context_limit",
    "visual_input_invalid",
)
_register(
    FailureSpec(ErrorCategory.VALIDATION, "creature_identity_unresolved", "Choose an exact verified creature name before starting the encounter.", 409, RecoveryAction.FIX_INPUT),
)
_register(
    FailureSpec(ErrorCategory.VALIDATION, "turn_identity_conflict", "This action identifier already belongs to a different action. Check its saved result before continuing.", 409, RecoveryAction.CHECK_EXISTING_RESULT),
)
_register(
    FailureSpec(ErrorCategory.ASSIGNMENT, "character_not_assigned", "Choose a character assigned to this campaign before continuing.", 409, RecoveryAction.ASSIGN_CHARACTER),
    "assignment_required", "character_assignment_required", "assignment_conflict",
    "choose_character", "character_assignment_conflict",
)
_register(
    FailureSpec(ErrorCategory.BUSY, "busy", "This action is still being processed. Check its status before trying again.", 409, RecoveryAction.WAIT),
    "session_busy", "turn_busy", "request_busy", "lock_conflict", "preparation_busy",
    "preparation_in_progress",
)
_register(
    FailureSpec(ErrorCategory.PROVIDER_UNAVAILABLE, "provider_unavailable", "The Dungeon Master service could not complete this request.", 503, RecoveryAction.CHECK_EXISTING_RESULT, True),
    "llm_unavailable", "provider_request_failed", "provider_unavailable",
)
_register(
    FailureSpec(ErrorCategory.RATE_LIMITED, "rate_limited", "The Dungeon Master service is receiving too many requests. Check the result before retrying.", 429, RecoveryAction.CHECK_EXISTING_RESULT, True),
    "rate_limited", "provider_rate_limited",
)
_register(
    FailureSpec(ErrorCategory.QUOTA_EXCEEDED, "quota_exceeded", "The configured service allowance has been reached. Review the provider setup before continuing.", 429, RecoveryAction.CONFIGURE_PROVIDER),
    "quota_exceeded", "provider_quota_exceeded", "budget_exhausted", "run_budget_exhausted",
    "attempt_budget_exhausted", "provider_budget_exhausted", "providerbudgetexhausted",
    "task_cost_reservation",
)
_register(
    FailureSpec(ErrorCategory.PROVIDER_CONFIGURATION, "provider_configuration", "The Dungeon Master service needs configuration before it can continue.", 503, RecoveryAction.CONFIGURE_PROVIDER),
    "provider_configuration", "provider_config_invalid", "preparation_profile_invalid", "model_policy",
)
_register(
    FailureSpec(ErrorCategory.SOURCE, "preparation_failed", "Adventure preparation needs attention. Review its setup and restart preparation to continue.", 409, RecoveryAction.RESTART_PREPARATION),
)
_register(
    FailureSpec(ErrorCategory.VALIDATION, "campaign_ended", "This campaign has ended. You can read its saved history.", 409, RecoveryAction.READ_HISTORY),
    "completed",
)
_register(
    FailureSpec(ErrorCategory.INTERNAL, "readiness_unavailable", "Campaign readiness could not be confirmed. Try again after checking the saved campaign status.", 500, RecoveryAction.CHECK_EXISTING_RESULT),
)
_register(
    FailureSpec(ErrorCategory.CANCELLED, "cancelled", "Preparation was cancelled. Your saved work is retained.", 409, RecoveryAction.RESTART_PREPARATION),
    "preparation_cancelled", "run_cancelled", "task_cancelled",
)
_register(
    FailureSpec(ErrorCategory.INTERRUPTED, "interrupted", "The connection stopped before the result was confirmed. Check the saved result before retrying.", 503, RecoveryAction.CHECK_EXISTING_RESULT, True),
    "incomplete", "turn_interrupted", "preparation_interrupted",
)
_register(
    FailureSpec(ErrorCategory.INTERRUPTED, "turn_not_found", "The server confirms this action was not saved. You can retry it safely.", 404, RecoveryAction.CHECK_EXISTING_RESULT, True),
)
_register(
    FailureSpec(ErrorCategory.NOT_FOUND, "not_found", "That saved item could not be found. Refresh and check that it is still available.", 404, RecoveryAction.FIX_INPUT),
)
_register(
    FailureSpec(
        ErrorCategory.INTERNAL,
        "turn_save_failed",
        "The turn could not be saved. Check the saved result before trying again.",
        500,
        RecoveryAction.CHECK_EXISTING_RESULT,
    ),
)
_register(
    FailureSpec(ErrorCategory.INTERNAL, "turn_failed", "Something went wrong while processing this action. Check the saved result before trying again.", 500, RecoveryAction.CONTACT_SUPPORT),
    "internal_error", "internal_fault", "task_execution_failed",
    "resolved_model_mismatch", "provider_token_limit", "provider_output_invalid",
    "provider_usage_missing", "unexpected_provider_call", "task_reservation_exceeded",
    "artifact_binding_invalid", "artifact_scope_invalid", "artifact_scope_incomplete",
    "validator_missing", "validator_contract_invalid", "validation_receipt_missing",
    "validation_receipt_binding_invalid", "stage_contract_missing", "stage_plan_invalid",
    "publication_callback_missing", "publication_failed", "lease_expired",
    "worker_cancelled", "worker_result_invalid", "generation_incomplete", "generation_error",
    "ingestion_error",
)

_DEFAULT = _SPECS["internal_fault"]


@dataclass(frozen=True, slots=True)
class SafeFailure:
    """A fixed response projection with no raw exception or provider text."""

    spec: FailureSpec
    outcome: OutcomeState
    support_ref: str

    @property
    def category(self) -> ErrorCategory:
        return self.spec.category

    @property
    def code(self) -> str:
        return self.spec.code

    @property
    def message(self) -> str:
        return self.spec.message

    @property
    def status_code(self) -> int:
        return self.spec.status_code

    @property
    def recovery_action(self) -> RecoveryAction:
        if self.spec.repair_action == RecoveryAction.CHECK_EXISTING_RESULT:
            if self.outcome == OutcomeState.NOT_COMMITTED:
                return RecoveryAction.RETRY if self.spec.retryable_when_uncommitted else RecoveryAction.CONTACT_SUPPORT
            if self.outcome == OutcomeState.COMMITTED:
                return RecoveryAction.CHECK_EXISTING_RESULT
            return RecoveryAction.CHECK_EXISTING_RESULT
        return self.spec.repair_action

    @property
    def retryable(self) -> bool:
        return self.recovery_action == RecoveryAction.RETRY

    def to_payload(self) -> dict[str, Any]:
        """Return fields compatible with existing code/message/detail consumers."""
        return {
            "code": self.code,
            "category": self.category.value,
            "message": self.message,
            "detail": self.message,
            "recovery_action": self.recovery_action.value,
            "retryable": self.retryable,
            "support_ref": self.support_ref,
            "outcome": self.outcome.value,
        }

    def log_fields(self) -> dict[str, str | bool]:
        """Safe correlated fields suitable for a structured log; no exception is retained."""
        return {
            "category": self.category.value,
            "support_ref": self.support_ref,
            "outcome": self.outcome.value,
            "retryable": self.retryable,
        }


def safe_code(value: object) -> str | None:
    """Return a fixed code only if it belongs to the taxonomy."""
    return value if isinstance(value, str) and value in _SPECS else None


def _provider_spec(exc: BaseException) -> FailureSpec | None:
    """Classify provider failures by SDK class/status, never message or response body."""
    try:
        from openai import (  # type: ignore[import-not-found]
            APIConnectionError,
            APIStatusError,
            AuthenticationError,
            BadRequestError,
            PermissionDeniedError,
            RateLimitError,
        )
    except ImportError:  # The core package is also used without provider extras.
        return None

    if isinstance(exc, RateLimitError):
        # The provider body is consulted only for a tiny, fixed enum. It is never copied,
        # stringified, logged, or returned to a caller.
        body = getattr(exc, "body", None)
        provider_code = None
        if isinstance(body, dict):
            nested = body.get("error")
            provider_code = nested.get("code") if isinstance(nested, dict) else body.get("code")
        if isinstance(provider_code, str) and provider_code in {"insufficient_quota", "billing_hard_limit_reached"}:
            return _SPECS["quota_exceeded"]
        return _SPECS["rate_limited"]
    if isinstance(exc, APIConnectionError):
        return _SPECS["provider_unavailable"]
    if isinstance(exc, (AuthenticationError, PermissionDeniedError, BadRequestError)):
        return _SPECS["provider_configuration"]
    if isinstance(exc, APIStatusError):
        status = getattr(exc, "status_code", None)
        if status == 429:
            body = getattr(exc, "body", None)
            provider_code = None
            if isinstance(body, dict):
                nested = body.get("error")
                provider_code = nested.get("code") if isinstance(nested, dict) else body.get("code")
            if isinstance(provider_code, str) and provider_code in {"insufficient_quota", "billing_hard_limit_reached"}:
                return _SPECS["quota_exceeded"]
            return _SPECS["rate_limited"]
        if isinstance(status, int) and status >= 500:
            return _SPECS["provider_unavailable"]
        if status in (401, 403, 400, 404, 422):
            return _SPECS["provider_configuration"]
    return None


def provider_error_code(exc: BaseException) -> str:
    """Return a safe stable code for an agent node before it discards exception identity."""
    spec = _provider_spec(exc)
    return spec.code if spec else _DEFAULT.code


def classify_failure(
    error: BaseException | str | None,
    *,
    outcome: OutcomeState | str = OutcomeState.UNKNOWN,
    support_ref: str | None = None,
) -> SafeFailure:
    """Classify a known internal code or exception without reading free-form text."""
    try:
        normalized_outcome = OutcomeState(outcome)
    except ValueError:
        normalized_outcome = OutcomeState.UNKNOWN

    spec = _SPECS.get(error) if isinstance(error, str) else None
    current = error if isinstance(error, BaseException) else None
    if isinstance(current, asyncio.CancelledError):
        spec = _SPECS["cancelled"]
    elif current is not None:
        provider_spec = _provider_spec(current)
        if provider_spec:
            spec = provider_spec
        else:
            code = safe_code(getattr(current, "code", None))
            if code:
                spec = _SPECS[code]
                # Only this fixed application wrapper is allowed to unwrap a provider
                # exception. Implicit exception context from an internal bug is not evidence
                # of provider unavailability.
                if code == "provider_request_failed":
                    cause = current.__cause__
                    if cause is not None:
                        nested_provider_spec = _provider_spec(cause)
                        if nested_provider_spec:
                            spec = nested_provider_spec
            else:
                spec = _DEFAULT

    if spec is None:
        spec = _DEFAULT
    ref = support_ref if isinstance(support_ref, str) and _valid_support_ref(support_ref) else None
    if ref is None:
        import secrets

        ref = f"err-{secrets.token_hex(8)}"
    return SafeFailure(spec, normalized_outcome, ref)


def _valid_support_ref(value: str) -> bool:
    return len(value) == 20 and value.startswith("err-") and all(
        char in "0123456789abcdef" for char in value[4:]
    )
