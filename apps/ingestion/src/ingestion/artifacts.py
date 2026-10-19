"""Lightweight canonical artifact hashes; safe to import without loading PDF/OCR models."""

import hashlib
import json
from typing import NotRequired, TypedDict

from edgar_core.artifacts import source_digest as source_digest, text_digest as text_digest


class ExcludedPage(TypedDict):
    """A reviewed non-content page; reasons are validated against EXCLUSION_REASONS."""

    page: int
    reason: str


class TextCorrection(TypedDict):
    """A reviewed, operator-recorded replacement of exact text on one physical page."""

    page: int
    old: str
    new: str
    note: NotRequired[str]


def exclusions_digest(excluded_pages: list[ExcludedPage]) -> str:
    """Canonical digest of reviewed page exclusions; any change is a new index identity."""
    canonical = json.dumps(excluded_pages, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def corrections_digest(text_corrections: list[TextCorrection]) -> str:
    """Canonical digest of reviewed text corrections; any change needs review and re-indexing."""
    canonical = json.dumps(text_corrections, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()
