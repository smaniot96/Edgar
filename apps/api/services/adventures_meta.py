"""Adventure metadata store (JSON beside the /data PDF store).

Shared by the adventures router and the background ingest/generation workers so neither has to
import the other. Metadata persists across DB resets and tracks the ingestion lifecycle
(processing -> ready | failed) plus title/description/chunk count.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import structlog

log = structlog.get_logger()

DATA_DIR = Path("/data")
ADVENTURES_DIR = DATA_DIR / "adventures"
PDF_DIR = DATA_DIR / "pdfs"


def meta_path(slug: str) -> Path:
    return ADVENTURES_DIR / f"{slug}.json"


def load_meta(slug: str) -> dict:
    p = meta_path(slug)
    if p.is_file():
        try:
            return json.loads(p.read_text())
        except Exception:
            log.warning("adventure_metadata_parse_error", slug=slug, path=str(p))
    return {}


def _write_meta(slug: str, data: dict, *, create: bool = False) -> None:
    """Publish complete JSON atomically; `create` must not replace an existing reservation."""
    ADVENTURES_DIR.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=ADVENTURES_DIR, suffix=".tmp", delete=False
        ) as file:
            temporary = Path(file.name)
            json.dump(data, file, indent=2)
        if create:
            # Linking a completed temporary file claims the name without overwriting it.
            # Competing API processes receive FileExistsError and choose another slug.
            os.link(temporary, meta_path(slug))
        else:
            temporary.replace(meta_path(slug))
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def save_meta(slug: str, data: dict) -> None:
    """Replace metadata without exposing a partially written JSON document to readers."""
    _write_meta(slug, data)


def reserve_meta(slug: str, data: dict) -> bool:
    """Create a complete reservation only if the slug is still free."""
    try:
        _write_meta(slug, data, create=True)
    except FileExistsError:
        return False
    return True


def metadata_slugs() -> set[str]:
    if not ADVENTURES_DIR.is_dir():
        return set()
    return {p.stem for p in ADVENTURES_DIR.glob("*.json")}


STALE_PROCESSING_ERROR = (
    "Processing was interrupted by a server restart. Use retry to process it again."
)


def fail_stale_processing() -> list[str]:
    """Mark every adventure still in "processing" as failed; return the affected slugs.

    Call once at startup: ingestion/generation run as in-process background tasks, so anything
    still "processing" when the process starts belongs to a run that no longer exists.
    """
    stale: list[str] = []
    for slug in sorted(metadata_slugs()):
        meta = load_meta(slug)
        if meta.get("status") == "processing":
            meta.update({"status": "failed", "error": STALE_PROCESSING_ERROR})
            save_meta(slug, meta)
            stale.append(slug)
    return stale
