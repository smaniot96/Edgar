"""Explicit cross-origin (CORS) and trusted-host policy (EDG-052).

Same-origin is the default and needs no configuration: the bundled UI at `/ui` and the Vite dev
proxy both reach the API on the same origin, so no CORS headers are sent. Separately hosted
clients are allowed only when `EDGAR_ALLOWED_ORIGINS` lists their exact origins.

Validation is fail-closed and runs when `apps.api.main` is imported, so a malformed value stops the
process before it serves a request. Errors name the setting only, never its value.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from edgar_core import config

# Exactly what the bundled client uses: fetch GET, POST and DELETE; JSON bodies (Content-Type, which
# is not CORS-safelisted, so it needs a preflight); Accept for SSE; Authorization for bearer tokens
# (EDG-065). X-Request-ID is accepted so a separate client can correlate its logs with ours.
# Last-Event-ID resumes a session channel subscription (EDG-047); it is not CORS-safelisted.
ALLOWED_METHODS = ("GET", "POST", "DELETE")
ALLOWED_REQUEST_HEADERS = ("Authorization", "Content-Type", "Accept", "X-Request-ID", "Last-Event-ID")
# The bundled client reads `x-request-id` from responses. It does not read Retry-After, so that stays
# unexposed until a client needs it.
EXPOSED_RESPONSE_HEADERS = ("X-Request-ID",)
PREFLIGHT_MAX_AGE_SECONDS = 600

_ORIGIN_SETTING = "EDGAR_ALLOWED_ORIGINS"
_CREDENTIALS_SETTING = "EDGAR_ALLOW_CREDENTIALS"
_HOSTS_SETTING = "EDGAR_TRUSTED_HOSTS"
_DEFAULT_PORTS = {"http": 80, "https": 443}
_DNS_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


class OriginConfigurationError(ValueError):
    """Startup failure. The message names the setting and never echoes its value."""


@dataclass(frozen=True)
class OriginPolicy:
    allowed_origins: tuple[str, ...] = ()
    allow_credentials: bool = False
    trusted_hosts: tuple[str, ...] = ()


def _split_entries(setting: str, raw: str) -> list[str]:
    if not raw.strip():
        return []
    entries = [entry.strip() for entry in raw.split(",")]
    if any(not entry for entry in entries):
        raise OriginConfigurationError(
            f"{setting} contains an empty entry; remove the extra comma."
        )
    return entries


def _normalize_origin(entry: str) -> str:
    """Return the exact serialized origin a browser sends, or fail closed."""
    malformed = OriginConfigurationError(
        f"{_ORIGIN_SETTING} entries must be exact origins such as https://ui.example.com "
        "(scheme, host and optional port; no path, query, fragment or credentials)."
    )
    if "*" in entry:
        raise OriginConfigurationError(
            f"{_ORIGIN_SETTING} must list exact origins; wildcards are not allowed, "
            f"even with {_CREDENTIALS_SETTING}."
        )
    if entry.lower() == "null" or any(ch.isspace() for ch in entry):
        raise malformed
    try:
        parts = urlsplit(entry)
        port = parts.port  # ValueError for a non-numeric or out-of-range port
    except ValueError as exc:
        raise malformed from exc
    scheme = parts.scheme.lower()
    host = parts.hostname
    if scheme not in _DEFAULT_PORTS:
        raise OriginConfigurationError(f"{_ORIGIN_SETTING} entries must use http:// or https://.")
    if not host or port == 0 or parts.username is not None or parts.password is not None:
        raise malformed
    if parts.path or parts.query or parts.fragment or "?" in entry or "#" in entry:
        raise malformed
    if ":" in host:
        try:
            ipaddress.IPv6Address(host)
        except ValueError as exc:
            raise malformed from exc
        host_part = f"[{host}]"
    else:
        if not all(_DNS_LABEL.match(label) for label in host.split(".")):
            raise malformed
        host_part = host
    # Browsers omit a default port from the Origin header, so an entry must match that form.
    if port is None or port == _DEFAULT_PORTS[scheme]:
        return f"{scheme}://{host_part}"
    return f"{scheme}://{host_part}:{port}"


def _parse_credentials_flag(raw: str) -> bool:
    value = raw.strip()
    if value in ("", "0"):
        return False
    if value == "1":
        return True
    raise OriginConfigurationError(f"{_CREDENTIALS_SETTING} must be 0 or 1.")


def _parse_trusted_host(entry: str) -> str:
    host = entry.lower()
    if "*" in host or not all(_DNS_LABEL.match(label) for label in host.split(".")):
        raise OriginConfigurationError(
            f"{_HOSTS_SETTING} entries must be bare host names (no wildcard, scheme, port, "
            "path or IPv6 literal)."
        )
    return host


def parse_origin_policy(
    allowed_origins: str = "", allow_credentials: str = "", trusted_hosts: str = ""
) -> OriginPolicy:
    """Validate the three raw settings. Empty inputs give the same-origin default."""
    origins: list[str] = []
    for entry in _split_entries(_ORIGIN_SETTING, allowed_origins):
        origin = _normalize_origin(entry)
        if origin not in origins:
            origins.append(origin)
    credentials = _parse_credentials_flag(allow_credentials)
    if credentials and not origins:
        raise OriginConfigurationError(
            f"{_CREDENTIALS_SETTING}=1 requires {_ORIGIN_SETTING} to list exact origins."
        )
    hosts: list[str] = []
    for entry in _split_entries(_HOSTS_SETTING, trusted_hosts):
        host = _parse_trusted_host(entry)
        if host not in hosts:
            hosts.append(host)
    return OriginPolicy(tuple(origins), credentials, tuple(hosts))


def origin_policy_from_config() -> OriginPolicy:
    """Read the settings at call time so tests and reloads see the current configuration."""
    return parse_origin_policy(
        config.EDGAR_ALLOWED_ORIGINS, config.EDGAR_ALLOW_CREDENTIALS, config.EDGAR_TRUSTED_HOSTS
    )


def origin_policy_summary(policy: OriginPolicy) -> dict[str, int | bool]:
    """Counts only, so startup logs never record the configured hosts."""
    return {
        "cors_origins": len(policy.allowed_origins),
        "cors_credentials": policy.allow_credentials,
        "trusted_hosts": len(policy.trusted_hosts),
    }


def apply_origin_policy(app: FastAPI, policy: OriginPolicy) -> None:
    """Install the policy. Add after other middleware so CORS sits outermost."""
    if policy.allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(policy.allowed_origins),
            allow_credentials=policy.allow_credentials,
            allow_methods=list(ALLOWED_METHODS),
            allow_headers=list(ALLOWED_REQUEST_HEADERS),
            expose_headers=list(EXPOSED_RESPONSE_HEADERS),
            max_age=PREFLIGHT_MAX_AGE_SECONDS,
        )
    if policy.trusted_hosts:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(policy.trusted_hosts))
