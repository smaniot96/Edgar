"""Adventures registry: list installed Qdrant adventure modules + per-adventure metadata.

Phase 1: GET /adventures
Phase 12: GET /adventures/{slug}, PATCH /adventures/{slug}, DELETE /adventures/{slug}
"""

import json
import shutil
from pathlib import Path

import structlog
from fastapi import APIRouter, Header, HTTPException
from qdrant_client.http.exceptions import UnexpectedResponse

from db.vector.client import get_qdrant_client
from db.vector.collections import is_campaign_lore_collection, is_rules_collection

from ..schemas.adventure import AdventureRead, AdventureUpdate

log = structlog.get_logger()
router = APIRouter(tags=["adventures"])

# Metadata lives beside the PDF store so it survives DB resets.
_ADVENTURES_DIR = Path(__file__).resolve().parents[4] / "data" / "adventures"


def _metadata_path(slug: str) -> Path:
    return _ADVENTURES_DIR / f"{slug}.json"


def load_adventure_metadata(slug: str) -> dict:
    """Read data/adventures/<slug>.json; return {} when absent."""
    p = _metadata_path(slug)
    if p.is_file():
        try:
            return json.loads(p.read_text())
        except Exception:
            log.warning("adventure_metadata_parse_error", slug=slug, path=str(p))
    return {}


def _save_adventure_metadata(slug: str, data: dict) -> None:
    _ADVENTURES_DIR.mkdir(parents=True, exist_ok=True)
    _metadata_path(slug).write_text(json.dumps(data, indent=2))


def _adventure_collections() -> list[str]:
    """Return sorted non-rules, non-lore Qdrant collection names. 503 if Qdrant is down."""
    client = get_qdrant_client()
    try:
        names = sorted(c.name for c in client.get_collections().collections)
    except Exception as exc:
        log.error("qdrant_unavailable", error=str(exc))
        raise HTTPException(
            status_code=503,
            detail="Vector store unavailable; cannot list adventures.",
        ) from exc
    return [n for n in names if not is_rules_collection(n) and not is_campaign_lore_collection(n)]


@router.get("/adventures", response_model=list[AdventureRead])
def list_adventures():
    """Return all adventure module collections with their metadata."""
    slugs = _adventure_collections()
    out: list[AdventureRead] = []
    for slug in slugs:
        meta = load_adventure_metadata(slug)
        out.append(
            AdventureRead(
                slug=slug,
                title=meta.get("title") or slug.replace("_", " ").title(),
                description=meta.get("description"),
                level_range=meta.get("level_range"),
                cover_image=meta.get("cover_image"),
            )
        )
    return out


@router.get("/adventures/{slug}", response_model=AdventureRead)
def get_adventure(slug: str):
    """Return metadata for a single adventure slug."""
    slugs = _adventure_collections()
    if slug not in slugs:
        raise HTTPException(status_code=404, detail="Adventure not found")
    meta = load_adventure_metadata(slug)
    return AdventureRead(
        slug=slug,
        title=meta.get("title") or slug.replace("_", " ").title(),
        description=meta.get("description"),
        level_range=meta.get("level_range"),
        cover_image=meta.get("cover_image"),
    )


@router.patch("/adventures/{slug}", response_model=AdventureRead)
def update_adventure(slug: str, body: AdventureUpdate):
    """Upsert adventure metadata (title, description, level_range, cover_image)."""
    slugs = _adventure_collections()
    if slug not in slugs:
        raise HTTPException(status_code=404, detail="Adventure not found")
    meta = load_adventure_metadata(slug)
    update = body.model_dump(exclude_unset=True)
    meta.update(update)
    _save_adventure_metadata(slug, meta)
    return AdventureRead(
        slug=slug,
        title=meta.get("title") or slug.replace("_", " ").title(),
        description=meta.get("description"),
        level_range=meta.get("level_range"),
        cover_image=meta.get("cover_image"),
    )


@router.delete("/adventures/{slug}", status_code=204)
def delete_adventure(slug: str, x_confirm_delete: str | None = Header(default=None)):
    """Drop the Qdrant collection and its metadata file.

    Requires `X-Confirm-Delete: yes` header to prevent accidental deletions.
    """
    if x_confirm_delete != "yes":
        raise HTTPException(
            status_code=400,
            detail="Send header X-Confirm-Delete: yes to confirm deletion.",
        )
    slugs = _adventure_collections()
    if slug not in slugs:
        raise HTTPException(status_code=404, detail="Adventure not found")
    client = get_qdrant_client()
    try:
        client.delete_collection(slug)
    except UnexpectedResponse as exc:
        raise HTTPException(status_code=502, detail=f"Qdrant error: {exc}") from exc

    meta_path = _metadata_path(slug)
    if meta_path.is_file():
        meta_path.unlink()

    cover_dir = _ADVENTURES_DIR / slug
    if cover_dir.is_dir():
        shutil.rmtree(cover_dir, ignore_errors=True)
