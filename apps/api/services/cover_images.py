"""Validation and normalization for public adventure cover references.

Adventure covers are optional browser-visible artwork. Metadata may name an asset below
``frontend/public/assets`` or a same-origin API resource. It must never name an arbitrary
filesystem path, a remote URL, or a browser-executable data URL.
"""

from __future__ import annotations

import re

_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")
_ASSET_PREFIXES = ("assets/", "ui/assets/", "/ui/assets/")


def normalize_cover_image(value: str | None) -> str | None:
    """Return the canonical local URL form or raise ``ValueError`` for unsafe input.

    Supported metadata values are ``assets/<path>`` (served below the SPA base), legacy
    ``ui/assets/<path>`` or ``/ui/assets/<path>``, and ``api/<path>`` or ``/api/<path>``.
    API references stay rooted at the API origin/path and are never placed below ``/ui``.
    """
    if value is None:
        return None
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 1024:
        raise ValueError("Cover image must be a non-empty local asset path.")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("Cover image contains unsupported characters.")
    if any(char in value for char in ("\\", "%", "?", "#", ":")):
        raise ValueError("Cover image must not contain URL schemes, escapes, or suffixes.")

    prefix: str | None = None
    canonical_prefix = ""
    for candidate in _ASSET_PREFIXES:
        if value.startswith(candidate):
            prefix = candidate
            canonical_prefix = "assets/" if candidate == "assets/" else "/ui/assets/"
            break
    if prefix is None:
        if value.startswith("/api/"):
            prefix = "/api/"
            canonical_prefix = "/api/"
        elif value.startswith("api/"):
            prefix = "api/"
            canonical_prefix = "/api/"
        else:
            raise ValueError("Cover image must use assets/ or /api/.")

    relative = value[len(prefix) :]
    segments = relative.split("/")
    if not relative or len(segments) > 32 or any(not _SEGMENT.fullmatch(part) for part in segments):
        raise ValueError("Cover image path contains an invalid component.")
    if any(part in {".", ".."} for part in segments):
        raise ValueError("Cover image path traversal is not allowed.")
    return canonical_prefix + relative
