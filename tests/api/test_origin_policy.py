"""Origin policy validation (EDG-052): fail-closed parsing with setting-only error messages.

Pure-function tests: no app, no database, no network.
"""

from __future__ import annotations

import pytest

from apps.api.origin_policy import (
    OriginConfigurationError,
    OriginPolicy,
    origin_policy_summary,
    parse_origin_policy,
)


def test_empty_settings_are_same_origin_only() -> None:
    policy = parse_origin_policy("", "", "")
    assert policy == OriginPolicy()
    assert policy.allowed_origins == ()
    assert policy.allow_credentials is False
    assert policy.trusted_hosts == ()


def test_whitespace_only_settings_are_the_default() -> None:
    assert parse_origin_policy("  ", " ", "\t") == OriginPolicy()


def test_exact_origins_are_normalised_and_deduplicated() -> None:
    policy = parse_origin_policy(
        "HTTPS://UI.Example.com, https://ui.example.com:443,http://localhost:5173",
        "",
        "",
    )
    # Browsers send lowercase origins without default ports; the default port is dropped here.
    assert policy.allowed_origins == ("https://ui.example.com", "http://localhost:5173")


def test_ipv6_origin_is_accepted_in_bracket_form() -> None:
    policy = parse_origin_policy("http://[::1]:5173", "", "")
    assert policy.allowed_origins == ("http://[::1]:5173",)


@pytest.mark.parametrize(
    "bad",
    [
        "*",
        "https://*.example.com",
        "https://ui.*.example.com",
        "null",
        "NULL",
        "ftp://ui.example.com",
        "file:///tmp/ui",
        "ui.example.com",
        "//ui.example.com",
        "https://ui.example.com/",
        "https://ui.example.com/app",
        "https://ui.example.com?x=1",
        "https://ui.example.com#frag",
        "https://user:pw@ui.example.com",
        "https://ui.example.com:notaport",
        "https://ui.example.com:0",
        "https://ui.example.com:99999",
        "https://ui example.com",
        "https://ui_example.com",
        "https://[::1",
        "https://[fe80::1%eth0x",
    ],
)
def test_malformed_origin_is_rejected(bad: str) -> None:
    with pytest.raises(OriginConfigurationError) as info:
        parse_origin_policy(bad, "", "")
    assert "EDGAR_ALLOWED_ORIGINS" in str(info.value)


def test_wildcard_inside_a_valid_list_is_rejected() -> None:
    with pytest.raises(OriginConfigurationError, match="EDGAR_ALLOWED_ORIGINS"):
        parse_origin_policy("https://ui.example.com, *", "", "")


def test_empty_list_entry_is_rejected() -> None:
    with pytest.raises(OriginConfigurationError, match="EDGAR_ALLOWED_ORIGINS"):
        parse_origin_policy("https://ui.example.com,,https://b.example.com", "", "")


def test_wildcard_with_credentials_is_rejected_and_names_both_settings_safely() -> None:
    with pytest.raises(OriginConfigurationError) as info:
        parse_origin_policy("*", "1", "")
    message = str(info.value)
    assert "EDGAR_ALLOWED_ORIGINS" in message
    assert "wildcards are not allowed" in message


@pytest.mark.parametrize("bad", ["true", "yes", "2", "on", "01"])
def test_credentials_flag_accepts_only_zero_or_one(bad: str) -> None:
    with pytest.raises(OriginConfigurationError, match="EDGAR_ALLOW_CREDENTIALS must be 0 or 1"):
        parse_origin_policy("https://ui.example.com", bad, "")


def test_credentials_without_origins_is_rejected() -> None:
    with pytest.raises(OriginConfigurationError, match="EDGAR_ALLOW_CREDENTIALS=1 requires"):
        parse_origin_policy("", "1", "")


def test_credentials_flag_is_opt_in_with_exact_origins() -> None:
    policy = parse_origin_policy("https://ui.example.com", "1", "")
    assert policy.allow_credentials is True
    assert parse_origin_policy("https://ui.example.com", "0", "").allow_credentials is False


@pytest.mark.parametrize(
    "bad",
    [
        "*",
        "*.example.com",
        "edgar.example.com:8000",
        "https://edgar.example.com",
        "edgar.example.com/path",
        "::1",
        "127.0.0.1:8000",
        "a..b",
    ],
)
def test_trusted_hosts_must_be_bare_host_names(bad: str) -> None:
    with pytest.raises(OriginConfigurationError) as info:
        parse_origin_policy("", "", bad)
    assert "EDGAR_TRUSTED_HOSTS" in str(info.value)


def test_trusted_hosts_are_lowercased_and_deduplicated() -> None:
    policy = parse_origin_policy("", "", "Edgar.Example.com, localhost,edgar.example.com")
    assert policy.trusted_hosts == ("edgar.example.com", "localhost")


def test_error_messages_never_echo_the_configured_value() -> None:
    secretish = "https://ops-internal.example.net/path-with-secret-token"
    with pytest.raises(OriginConfigurationError) as info:
        parse_origin_policy(secretish, "", "")
    assert "secret-token" not in str(info.value)
    assert "ops-internal" not in str(info.value)


def test_summary_reports_counts_only() -> None:
    policy = parse_origin_policy("https://ui.example.com", "1", "edgar.example.com")
    assert origin_policy_summary(policy) == {
        "cors_origins": 1,
        "cors_credentials": True,
        "trusted_hosts": 1,
    }
