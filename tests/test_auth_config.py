"""Auth mode selection and startup validation (EDG-065). Pure: no database, no Docker."""

from __future__ import annotations

import pytest

from apps.api.auth import (
    AuthConfigError,
    admin_user_ids,
    auth_mode,
    generate_token,
    hash_token,
    validate_auth_configuration,
)


def test_default_mode_is_local() -> None:
    assert auth_mode({}) == "local"
    assert auth_mode({"EDGAR_AUTH_MODE": ""}) == "local"
    assert auth_mode({"EDGAR_AUTH_MODE": "local"}) == "local"
    assert auth_mode({"EDGAR_AUTH_MODE": " LOCAL "}) == "local"


@pytest.mark.parametrize("raw", ["token", "TOKEN", " token ", "tokne", "open", "none"])
def test_anything_but_local_fails_closed_to_token(raw: str) -> None:
    # An unrecognised value must never silently fall back to the unauthenticated local mode.
    assert auth_mode({"EDGAR_AUTH_MODE": raw}) == "token"


def test_local_mode_validates_without_admin_configuration() -> None:
    assert validate_auth_configuration({}) == {"auth_mode": "local"}
    assert validate_auth_configuration({"EDGAR_AUTH_MODE": "local"}) == {"auth_mode": "local"}


def test_local_mode_ignores_debug_flag() -> None:
    assert validate_auth_configuration({"EDGAR_DEBUG": "1"}) == {"auth_mode": "local"}


def test_token_mode_with_admins_is_valid() -> None:
    env = {"EDGAR_AUTH_MODE": "token", "EDGAR_ADMIN_USER_IDS": "1, 7"}
    assert validate_auth_configuration(env) == {"auth_mode": "token", "admin_count": 2}
    assert admin_user_ids(env) == frozenset({1, 7})


def test_unknown_mode_is_rejected_by_name_only() -> None:
    with pytest.raises(AuthConfigError) as exc:
        validate_auth_configuration({"EDGAR_AUTH_MODE": "tokne"})
    assert "EDGAR_AUTH_MODE" in str(exc.value)
    assert "tokne" not in str(exc.value)


def test_token_mode_requires_admin_ids() -> None:
    with pytest.raises(AuthConfigError, match="EDGAR_ADMIN_USER_IDS"):
        validate_auth_configuration({"EDGAR_AUTH_MODE": "token"})
    with pytest.raises(AuthConfigError, match="EDGAR_ADMIN_USER_IDS"):
        validate_auth_configuration({"EDGAR_AUTH_MODE": "token", "EDGAR_ADMIN_USER_IDS": "  "})


@pytest.mark.parametrize("bad", ["abc", "0", "-1", "1,,2", "1,x", "2.5"])
def test_token_mode_rejects_malformed_admin_ids(bad: str) -> None:
    with pytest.raises(AuthConfigError) as exc:
        validate_auth_configuration({"EDGAR_AUTH_MODE": "token", "EDGAR_ADMIN_USER_IDS": bad})
    assert "positive integer" in str(exc.value)
    # The offending value is never echoed back.
    assert bad not in str(exc.value) or bad.isdigit()


def test_token_mode_refuses_debug_without_override() -> None:
    env = {"EDGAR_AUTH_MODE": "token", "EDGAR_ADMIN_USER_IDS": "1", "EDGAR_DEBUG": "1"}
    with pytest.raises(AuthConfigError, match="EDGAR_DEBUG"):
        validate_auth_configuration(env)


def test_token_mode_debug_override_is_explicit() -> None:
    env = {
        "EDGAR_AUTH_MODE": "token",
        "EDGAR_ADMIN_USER_IDS": "1",
        "EDGAR_DEBUG": "true",
        "EDGAR_ALLOW_DEBUG_IN_TOKEN_MODE": "1",
    }
    assert validate_auth_configuration(env)["auth_mode"] == "token"
    # A value other than exactly 1 is not an override.
    env["EDGAR_ALLOW_DEBUG_IN_TOKEN_MODE"] = "yes"
    with pytest.raises(AuthConfigError):
        validate_auth_configuration(env)


def test_debug_off_is_allowed_in_token_mode() -> None:
    env = {"EDGAR_AUTH_MODE": "token", "EDGAR_ADMIN_USER_IDS": "1", "EDGAR_DEBUG": "0"}
    assert validate_auth_configuration(env)["auth_mode"] == "token"


def test_token_digest_is_sha256_and_never_the_token() -> None:
    token = generate_token()
    digest = hash_token(token)
    assert len(digest) == 64
    int(digest, 16)  # hex
    assert digest != token
    assert token not in digest
    assert hash_token(token) == digest


def test_generated_tokens_are_prefixed_high_entropy_and_unique() -> None:
    tokens = {generate_token() for _ in range(50)}
    assert len(tokens) == 50
    for token in tokens:
        assert token.startswith("edgar_")
        assert len(token) >= 45  # 'edgar_' + 43 url-safe base64 characters (256 bits)
