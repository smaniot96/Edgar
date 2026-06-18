"""Adventures registry: upload + embed campaign PDFs, list installed modules, manage metadata.

An "adventure" is an embedded campaign module: a PDF that has been chunked, embedded, and
upserted into its own Qdrant collection (the slug). Players then start a Campaign from a ready
adventure (see routers/campaign.py). Uploads embed in the background; the metadata JSON beside
the PDF store tracks the lifecycle (processing -> ready | failed) so it survives DB resets.
"""

import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

import structlog
from fastapi import APIRouter, BackgroundTasks, File, Form, Header, HTTPException, UploadFile
from qdrant_client.http.exceptions import UnexpectedResponse

from db.vector.client import get_qdrant_client
from db.vector.collections import (
    is_campaign_lore_collection,
    is_rules_collection,
)

from ..schemas.adventure import (
    AdventureGenerateRequest,
    AdventureRead,
    AdventureUpdate,
    AdventureUploadResponse,
)
from ..services.adventures_meta import (
    ADVENTURES_DIR as _ADVENTURES_DIR,
    PDF_DIR as _PDF_DIR,
    load_meta as load_adventure_metadata,
    metadata_slugs,
    meta_path as _metadata_path,
    save_meta as _save_adventure_metadata,
)

log = structlog.get_logger()
router = APIRouter(tags=["adventures"])

_MAX_UPLOAD_BYTES = 64 * 1024 * 1024  # 64 MB


def _slugify(name: str) -> str:
    """A safe Qdrant collection name: lowercase, alnum + underscores."""
    base = re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")
    return base or "adventure"


def _all_collection_names() -> list[str]:
    client = get_qdrant_client()
    try:
        return [c.name for c in client.get_collections().collections]
    except Exception as exc:
        log.error("qdrant_unavailable", error=str(exc))
        raise HTTPException(
            status_code=503,
            detail="Vector store unavailable; cannot list adventures.",
        ) from exc


def _adventure_collection_names(all_names: list[str]) -> set[str]:
    return {n for n in all_names if not is_rules_collection(n) and not is_campaign_lore_collection(n)}


def _unique_slug(desired: str, taken: set[str]) -> str:
    slug = desired
    i = 2
    while slug in taken:
        slug = f"{desired}_{i}"
        i += 1
    return slug


def _read_for_slug(slug: str, has_collection: bool) -> AdventureRead:
    meta = load_adventure_metadata(slug)
    status = meta.get("status") or ("ready" if has_collection else "processing")
    return AdventureRead(
        slug=slug,
        title=meta.get("title") or slug.replace("_", " ").title(),
        description=meta.get("description"),
        level_range=meta.get("level_range"),
        cover_image=meta.get("cover_image"),
        status=status,
        chunks=meta.get("chunks"),
        source_filename=meta.get("source_filename"),
        error=meta.get("error"),
        created_at=meta.get("created_at"),
    )


@router.get("/adventures", response_model=list[AdventureRead])
def list_adventures():
    """All adventure modules: embedded collections plus any still-processing/failed uploads."""
    all_names = _all_collection_names()
    collection_slugs = _adventure_collection_names(all_names)

    # Include slugs that only have a metadata file (e.g. still processing/generating, or a
    # failed upload whose collection was never created).
    meta_slugs = metadata_slugs()

    out: list[AdventureRead] = []
    for slug in sorted(collection_slugs | meta_slugs):
        out.append(_read_for_slug(slug, has_collection=slug in collection_slugs))
    return out


@router.get("/adventures/{slug}", response_model=AdventureRead)
def get_adventure(slug: str):
    all_names = _all_collection_names()
    collection_slugs = _adventure_collection_names(all_names)
    if slug not in collection_slugs and not _metadata_path(slug).is_file():
        raise HTTPException(status_code=404, detail="Adventure not found")
    return _read_for_slug(slug, has_collection=slug in collection_slugs)


def _ingest_adventure(slug: str, pdf_path: str, title: str) -> None:
    """Background worker: embed the PDF and flip the adventure's status to ready/failed."""
    # Imported here so the API process only pays the ingestion import cost on first upload.
    from ingestion.embed import run_pipeline

    structlog.contextvars.bind_contextvars(adventure_slug=slug)
    try:
        n = run_pipeline(pdf_path=pdf_path, collection_name=slug, save_markdown=False)
        meta = load_adventure_metadata(slug)
        meta.update(
            {
                "status": "ready" if n > 0 else "failed",
                "chunks": n,
                "error": None if n > 0 else "No text could be extracted from the PDF.",
            }
        )
        _save_adventure_metadata(slug, meta)
        log.info("adventure_ingested", slug=slug, chunks=n)
    except Exception as exc:
        log.exception("adventure_ingest_failed", slug=slug)
        meta = load_adventure_metadata(slug)
        meta.update({"status": "failed", "error": str(exc)[:500]})
        _save_adventure_metadata(slug, meta)
    finally:
        structlog.contextvars.unbind_contextvars("adventure_slug")


@router.post("/adventures/upload", response_model=AdventureUploadResponse, status_code=202)
async def upload_adventure(
    background: BackgroundTasks,
    file: UploadFile = File(...),
    title: str | None = Form(default=None),
):
    """Upload a campaign PDF; it embeds in the background and becomes a playable adventure.

    Returns 202 immediately with status "processing"; poll GET /adventures for the slug to see
    it flip to "ready" (or "failed").
    """
    filename = file.filename or "adventure.pdf"
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported.")

    contents = await file.read()
    if not contents:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")
    if len(contents) > _MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"PDF too large (max {_MAX_UPLOAD_BYTES // (1024 * 1024)} MB).",
        )

    desired_title = (title or Path(filename).stem.replace("_", " ").strip()) or "Untitled Adventure"
    all_names = _all_collection_names()
    taken = set(all_names) | metadata_slugs()
    slug = _unique_slug(_slugify(desired_title), taken)

    _PDF_DIR.mkdir(parents=True, exist_ok=True)
    pdf_path = _PDF_DIR / f"{slug}.pdf"
    pdf_path.write_bytes(contents)

    _save_adventure_metadata(
        slug,
        {
            "title": desired_title,
            "status": "processing",
            "chunks": None,
            "error": None,
            "source_filename": filename,
            "created_at": datetime.now(timezone.utc).isoformat(),
        },
    )

    background.add_task(_ingest_adventure, slug, str(pdf_path), desired_title)
    log.info("adventure_upload_accepted", slug=slug, filename=filename, bytes=len(contents))
    return AdventureUploadResponse(slug=slug, title=desired_title, status="processing")


@router.post("/adventures/generate", response_model=AdventureUploadResponse, status_code=202)
async def generate_adventure(body: AdventureGenerateRequest, background: BackgroundTasks):
    """Have the AI author a full campaign in the background; it becomes a playable module.

    `size` (small | medium | large | gigantic) scales length/complexity. Returns 202 with
    status "processing"; poll GET /adventures for the slug. Large/gigantic take longer (many
    chapters are generated), so the card stays "processing" for a while.
    """
    from ..services.campaign_generator import build_generated_campaign

    title_hint = (body.title or "").strip()
    base = _slugify(title_hint or body.theme or "ai campaign")
    taken = set(_all_collection_names()) | metadata_slugs()
    slug = _unique_slug(base, taken)

    _save_adventure_metadata(
        slug,
        {
            "title": title_hint or "AI campaign (generating…)",
            "status": "processing",
            "chunks": None,
            "error": None,
            "source_filename": None,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "generation": {"kind": "ai", "size": body.size, "theme": body.theme},
        },
    )
    background.add_task(
        build_generated_campaign, slug, title_hint, body.theme or "", body.size
    )
    log.info("adventure_generate_accepted", slug=slug, size=body.size)
    return AdventureUploadResponse(
        slug=slug, title=title_hint or "AI campaign", status="processing"
    )


@router.patch("/adventures/{slug}", response_model=AdventureRead)
def update_adventure(slug: str, body: AdventureUpdate):
    """Upsert adventure metadata (title, description, level_range, cover_image)."""
    all_names = _all_collection_names()
    collection_slugs = _adventure_collection_names(all_names)
    if slug not in collection_slugs and not _metadata_path(slug).is_file():
        raise HTTPException(status_code=404, detail="Adventure not found")
    meta = load_adventure_metadata(slug)
    meta.update(body.model_dump(exclude_unset=True))
    _save_adventure_metadata(slug, meta)
    return _read_for_slug(slug, has_collection=slug in collection_slugs)


@router.delete("/adventures/{slug}", status_code=204)
def delete_adventure(slug: str, x_confirm_delete: str | None = Header(default=None)):
    """Drop the Qdrant collection, its metadata, the uploaded PDF, and any cover dir.

    Requires `X-Confirm-Delete: yes` header to prevent accidental deletions.
    """
    if x_confirm_delete != "yes":
        raise HTTPException(
            status_code=400,
            detail="Send header X-Confirm-Delete: yes to confirm deletion.",
        )
    all_names = _all_collection_names()
    collection_slugs = _adventure_collection_names(all_names)
    if slug not in collection_slugs and not _metadata_path(slug).is_file():
        raise HTTPException(status_code=404, detail="Adventure not found")

    if slug in collection_slugs:
        client = get_qdrant_client()
        try:
            client.delete_collection(slug)
        except UnexpectedResponse as exc:
            raise HTTPException(status_code=502, detail=f"Qdrant error: {exc}") from exc

    meta_path = _metadata_path(slug)
    if meta_path.is_file():
        meta_path.unlink()

    pdf_path = _PDF_DIR / f"{slug}.pdf"
    if pdf_path.is_file():
        pdf_path.unlink()

    cover_dir = _ADVENTURES_DIR / slug
    if cover_dir.is_dir():
        shutil.rmtree(cover_dir, ignore_errors=True)
