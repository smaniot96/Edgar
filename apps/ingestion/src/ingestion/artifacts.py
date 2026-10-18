"""Lightweight canonical artifact hashes; safe to import without loading PDF/OCR models."""

import hashlib
import json
from typing import TypedDict

from edgar_core.artifacts import source_digest as source_digest, text_digest as text_digest


class ExcludedPage(TypedDict):
    """A reviewed non-content page; reasons are validated against EXCLUSION_REASONS."""

    page: int
    reason: str


def exclusions_digest(excluded_pages: list[ExcludedPage]) -> str:
    """Canonical digest of reviewed page exclusions; any change is a new index identity."""
    canonical = json.dumps(excluded_pages, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()
