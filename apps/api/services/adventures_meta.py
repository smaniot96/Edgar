"""Adventure metadata store (JSON beside the /data PDF store).

Shared by the adventures router and the background ingest/generation workers so neither has to
import the other. Metadata persists across DB resets and tracks the ingestion lifecycle
(processing -> ready | failed) plus title/description/chunk count.
"""

from __future__ import annotations

import json
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


def save_meta(slug: str, data: dict) -> None:
    ADVENTURES_DIR.mkdir(parents=True, exist_ok=True)
    meta_path(slug).write_text(json.dumps(data, indent=2))


def metadata_slugs() -> set[str]:
    if not ADVENTURES_DIR.is_dir():
        return set()
    return {p.stem for p in ADVENTURES_DIR.glob("*.json")}
