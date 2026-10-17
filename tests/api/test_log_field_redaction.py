"""Field-level redaction is defense in depth; correlation fields must always survive it."""

import json
import os
import subprocess
import sys

import pytest

from apps.api.logging_config import FORBIDDEN_LOG_FIELDS, _redact, redact_sensitive_data

# Deliberately not token-shaped: CI runs gitleaks over the repository.
PRIVATE_PROSE = "The innkeeper whispers that the duke is the thief"
PRIVATE_KEY_VALUE = "example-not-a-real-key-value"

CORRELATION = {
    "request_id": "req-abc-123",
    "support_ref": "err-0123456789abcdef",
    "category": "provider_unavailable",
    "outcome": "not_committed",
    "retryable": True,
    "session_id": 42,
    "turn_id": "turn-7",
    "event": "turn_failed",
    "level": "error",
    "logger": "apps.api.session",
    "timestamp": "2026-10-17T10:00:00Z",
}


@pytest.mark.parametrize("key", sorted(FORBIDDEN_LOG_FIELDS))
def test_every_forbidden_top_level_key_is_redacted(key: str) -> None:
    event = {**CORRELATION, key: PRIVATE_PROSE}
    out = redact_sensitive_data(None, "error", event)
    assert out[key] == "[REDACTED]"
    assert PRIVATE_PROSE not in json.dumps(out)


@pytest.mark.parametrize("key", ["Narration", "PLAYER-INPUT", "api-key", "Set-Cookie"])
def test_redaction_normalises_case_and_dashes(key: str) -> None:
    out = _redact({key: PRIVATE_PROSE})
    assert out[key] == "[REDACTED]"


def test_forbidden_keys_are_redacted_inside_nested_extra_mappings() -> None:
    event = {
        **CORRELATION,
        "extra": {
            "narration": PRIVATE_PROSE,
            "monster_name": "Goblin",
            "nested": [{"prompt": PRIVATE_PROSE, "count": 3}],
        },
    }
    out = redact_sensitive_data(None, "info", event)
    assert out["extra"]["narration"] == "[REDACTED]"
    assert out["extra"]["monster_name"] == "Goblin"
    assert out["extra"]["nested"] == [{"prompt": "[REDACTED]", "count": 3}]
    assert PRIVATE_PROSE not in json.dumps(out)


def test_secret_style_keys_still_redacted() -> None:
    out = redact_sensitive_data(
        None, "warning", {**CORRELATION, "credentials": {"api-key": PRIVATE_KEY_VALUE}}
    )
    assert out["credentials"]["api-key"] == "[REDACTED]"
    assert PRIVATE_KEY_VALUE not in json.dumps(out)


def test_correlation_and_normal_fields_survive_redaction() -> None:
    event = {**CORRELATION, "narration": PRIVATE_PROSE, "chunks": 4, "slug": "lost-mine"}
    out = redact_sensitive_data(None, "error", event)
    for key, value in CORRELATION.items():
        assert out[key] == value, key
    assert out["chunks"] == 4
    assert out["slug"] == "lost-mine"


def test_token_count_fields_are_not_mistaken_for_credentials() -> None:
    # Exact key matching: budget_tokens and friends are numbers, not credentials.
    event = {**CORRELATION, "budget_tokens": 900, "fixed_tokens": 120, "tokens": 5}
    out = redact_sensitive_data(None, "info", event)
    assert out["budget_tokens"] == 900
    assert out["fixed_tokens"] == 120


def test_free_text_values_still_get_credential_patterns_redacted() -> None:
    value = (
        "failed calling https://svc-user:svc-pass@example.test/v1 with Bearer example.token.value"
    )
    out = redact_sensitive_data(None, "error", {**CORRELATION, "detail": value})
    assert "svc-pass" not in out["detail"]
    assert "example.token.value" not in out["detail"]


def test_redaction_end_to_end_through_json_stdout_keeps_correlation() -> None:
    # Exercise the real configured sink in a subprocess: logging state is process-wide.
    source = """
import structlog
import apps.api.logging_config as config
config.ENV = "production"
config.configure_logging()
structlog.contextvars.bind_contextvars(request_id="req-abc-123")
structlog.get_logger("api.test").error(
    "turn_failed",
    category="provider_unavailable",
    support_ref="err-0123456789abcdef",
    outcome="not_committed",
    narration=PRIVATE_PROSE,
    extra={"player_input": PRIVATE_PROSE, "prompt": PRIVATE_PROSE, "chunks": 2},
)
"""
    source = source.replace("PRIVATE_PROSE", repr(PRIVATE_PROSE))
    result = subprocess.run(
        [sys.executable, "-c", source],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ},
    )
    (line,) = result.stdout.splitlines()
    event = json.loads(line)
    assert event["request_id"] == "req-abc-123"
    assert event["support_ref"] == "err-0123456789abcdef"
    assert event["category"] == "provider_unavailable"
    assert event["outcome"] == "not_committed"
    assert event["narration"] == "[REDACTED]"
    assert event["extra"] == {"player_input": "[REDACTED]", "prompt": "[REDACTED]", "chunks": 2}
    assert "innkeeper" not in result.stdout
