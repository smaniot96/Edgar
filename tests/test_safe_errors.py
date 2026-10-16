"""Regressions for safe error classification and projection."""

from __future__ import annotations

import httpx
import structlog
from openai import APIConnectionError, RateLimitError

from apps.api.services.safe_errors import classify_http_failure, log_failure
from edgar_core.safe_errors import OutcomeState, classify_failure

PLAYER_SENTINEL = "the duke is secretly the thief"
SECRET_SENTINEL = "sk-test-super-secret-token"
PROVIDER_SENTINEL = "provider included private prompt data"


def test_real_connection_error_is_safe_provider_failure() -> None:
    request = httpx.Request("POST", "https://api.example.test/v1/responses")
    error = APIConnectionError(request=request)
    error.args = (f"{PLAYER_SENTINEL} {SECRET_SENTINEL}",)

    failure = classify_failure(error, support_ref="err-0123456789abcdef")
    payload = failure.to_payload()

    assert failure.category == "provider_unavailable"
    assert failure.recovery_action == "check_existing_result"
    assert payload["support_ref"] == "err-0123456789abcdef"
    assert PLAYER_SENTINEL not in repr(payload)
    assert SECRET_SENTINEL not in repr(payload)


def test_rate_limit_and_allowlisted_quota_are_distinct() -> None:
    request = httpx.Request("POST", "https://api.example.test/v1/responses")
    response = httpx.Response(
        429,
        request=request,
        json={"error": {"code": "insufficient_quota", "message": PROVIDER_SENTINEL}},
    )
    quota = RateLimitError(
        "private provider body",
        response=response,
        body={"error": {"code": "insufficient_quota", "message": PROVIDER_SENTINEL}},
    )
    rate = RateLimitError(
        "private provider body",
        response=response,
        body={"error": {"code": "rate_limit_exceeded", "message": PROVIDER_SENTINEL}},
    )

    assert classify_failure(quota).category == "quota_exceeded"
    assert classify_failure(rate).category == "rate_limited"
    assert PROVIDER_SENTINEL not in repr(classify_failure(quota).to_payload())


def test_explicit_provider_wrapper_unwraps_direct_sdk_cause_only() -> None:
    request = httpx.Request("POST", "https://api.example.test/v1/responses")
    provider_error = APIConnectionError(request=request)
    wrapped = RuntimeError(f"{PLAYER_SENTINEL} {SECRET_SENTINEL}")
    wrapped.code = "provider_request_failed"  # type: ignore[attr-defined]
    wrapped.__cause__ = provider_error

    failure = classify_failure(wrapped)

    assert failure.category == "provider_unavailable"
    assert PLAYER_SENTINEL not in repr(failure.to_payload())
    assert SECRET_SENTINEL not in repr(failure.to_payload())


def test_implicit_provider_context_does_not_hide_internal_type_error() -> None:
    request = httpx.Request("POST", "https://api.example.test/v1/responses")
    provider_error = APIConnectionError(request=request)
    internal = TypeError(f"{PLAYER_SENTINEL} {SECRET_SENTINEL}")
    internal.__context__ = provider_error

    failure = classify_failure(internal, outcome=OutcomeState.UNKNOWN)

    assert failure.category == "internal"
    assert failure.code == "turn_failed"
    assert PLAYER_SENTINEL not in repr(failure.to_payload())
    assert SECRET_SENTINEL not in repr(failure.to_payload())


def test_turn_save_failed_keeps_distinct_stable_code_and_check_action() -> None:
    failure = classify_failure("turn_save_failed", outcome=OutcomeState.UNKNOWN)

    assert failure.code == "turn_save_failed"
    assert failure.category == "internal"
    assert failure.recovery_action == "check_existing_result"
    assert failure.to_payload()["outcome"] == "unknown"


def test_structured_log_contains_no_exception_or_provider_text(fresh_safe_error_logger) -> None:
    error = APIConnectionError(
        request=httpx.Request("POST", "https://api.example.test/v1/responses")
    )
    error.args = (f"{PLAYER_SENTINEL} {SECRET_SENTINEL}",)
    failure = classify_failure(error, support_ref="err-0123456789abcdef")

    with structlog.testing.capture_logs() as captured:
        log_failure("turn_failed", failure, session_id=12, turn_id="turn_123")

    assert len(captured) == 1
    logged = repr(captured)
    assert "provider_unavailable" in logged
    assert "err-0123456789abcdef" in logged
    assert PLAYER_SENTINEL not in logged
    assert SECRET_SENTINEL not in logged


def test_unattributed_gateway_status_stays_interrupted() -> None:
    for status in (502, 503, 504):
        failure = classify_http_failure(status, outcome=OutcomeState.UNKNOWN)
        assert failure.category == "interrupted"
        assert failure.code == "interrupted"
        assert failure.recovery_action == "check_existing_result"


def test_explicit_http_category_overrides_generic_status_without_reading_detail() -> None:
    failure = classify_http_failure(
        503,
        code="required_books_unavailable",
        outcome=OutcomeState.NOT_COMMITTED,
    )

    assert failure.category == "source"
    assert failure.code == "campaign_sources_unavailable"
    assert "private" not in failure.message
