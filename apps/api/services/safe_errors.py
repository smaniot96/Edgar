"""API adapter for the shared safe error taxonomy."""

from __future__ import annotations

import re
import secrets
from typing import Any

import structlog

from edgar_core.safe_errors import (
    OutcomeState,
    SafeFailure,
    classify_failure,
)

log = structlog.get_logger()


def classify_error(
    error: BaseException | str | None,
    *,
    outcome: OutcomeState | str = OutcomeState.UNKNOWN,
    support_ref: str | None = None,
) -> SafeFailure:
    """Classify an API/provider failure without inspecting its free-form message."""
    return classify_failure(error, outcome=outcome, support_ref=support_ref)


def new_support_ref() -> str:
    """Create a stable identifier for one persisted failure event."""
    return f"err-{secrets.token_hex(8)}"


def safe_failure_metadata(
    error: BaseException | str | None,
    *,
    outcome: OutcomeState | str = OutcomeState.UNKNOWN,
    support_ref: str | None = None,
) -> dict[str, str]:
    """Return only fixed taxonomy values suitable for durable job metadata."""
    failure = classify_error(error, outcome=outcome, support_ref=support_ref or new_support_ref())
    return {
        "failure_code": failure.code,
        "error": failure.message,
        "support_ref": failure.support_ref,
    }


def classify_http_failure(
    status_code: int,
    *,
    code: str | None = None,
    outcome: OutcomeState | str = OutcomeState.UNKNOWN,
    support_ref: str | None = None,
) -> SafeFailure:
    """Map a FastAPI/HTTP failure without reading its detail or validation body."""
    from edgar_core.safe_errors import safe_code

    fixed_code = safe_code(code)
    if fixed_code is not None:
        return classify_error(fixed_code, outcome=outcome, support_ref=support_ref)
    if status_code == 404:
        fixed_code = "not_found"
    elif status_code == 422 or status_code == 400:
        fixed_code = "validation_failed"
    elif status_code == 409:
        fixed_code = "busy"
    elif status_code == 429:
        fixed_code = "rate_limited"
    elif status_code in (502, 503, 504):
        # Generic HTTP status alone does not prove the provider caused the failure.
        # Treat the result as uncertain and let the route pass a fixed provider code
        # only when it has evidence for that attribution.
        fixed_code = "turn_interrupted"
    else:
        fixed_code = "turn_failed"
    return classify_error(fixed_code, outcome=outcome, support_ref=support_ref)


def error_payload(
    error: BaseException | str | None,
    *,
    outcome: OutcomeState | str = OutcomeState.UNKNOWN,
    support_ref: str | None = None,
) -> dict[str, Any]:
    """Build the stable safe payload, retaining legacy `code`, `message`, and `detail`."""
    return classify_error(error, outcome=outcome, support_ref=support_ref).to_payload()


def http_failure_payload(failure: SafeFailure) -> dict[str, Any]:
    """Preserve safe historical gate envelopes alongside the shared top-level fields."""
    payload = failure.to_payload()
    if failure.category in {"source", "review"}:
        payload["detail"] = {"code": failure.code, "message": failure.message}
        if failure.code == "campaign_sources_unavailable":
            payload["detail"]["retrieval_degraded"] = True
    return payload


def log_failure(
    event: str,
    failure: SafeFailure,
    *,
    level: str = "error",
    **safe_context: Any,
) -> None:
    """Log correlation and category only; callers must not pass exception-derived values."""
    if level not in {"warning", "error", "info"}:
        level = "error"
    fields: dict[str, Any] = failure.log_fields()
    for key in ("session_id", "campaign_id"):
        value = safe_context.get(key)
        if type(value) is int and value > 0:
            fields[key] = value
    turn_id = safe_context.get("turn_id")
    if isinstance(turn_id, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", turn_id):
        fields["turn_id"] = turn_id
    getattr(log, level)(event, **fields)
