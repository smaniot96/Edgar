"""Adventures registry: upload + embed campaign PDFs, list installed modules, manage metadata.

An "adventure" is an embedded campaign module: a PDF that has been chunked, embedded, and
upserted into its own Qdrant collection (the slug). Players then start a Campaign from a ready
adventure (see routers/campaign.py). Uploads embed in the background; the metadata JSON beside
the PDF store tracks the lifecycle (processing -> ready | failed) so it survives DB resets.
"""

import fcntl
import json
import re
import shutil
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

import structlog
from fastapi import APIRouter, BackgroundTasks, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from qdrant_client.http.exceptions import UnexpectedResponse
from starlette.concurrency import run_in_threadpool

from db.vector.client import get_qdrant_client
from db.vector.collections import (
    is_campaign_lore_collection,
    is_rules_collection,
)

from ..schemas.adventure import (
    AdventureGenerateRequest,
    AdventureRead,
    AdventureRetryRequest,
    AdventureUpdate,
    AdventureUploadResponse,
)
from ..services.adventures_meta import (
    ADVENTURES_DIR as _ADVENTURES_DIR,
    PDF_DIR as _PDF_DIR,
    load_meta as load_adventure_metadata,
    meta_path as _metadata_path,
    metadata_slugs,
    reserve_meta,
    save_meta as _save_adventure_metadata,
)

log = structlog.get_logger()
router = APIRouter(tags=["adventures"])

_MAX_UPLOAD_BYTES = 64 * 1024 * 1024  # 64 MB
_COPY_CHUNK_BYTES = 1024 * 1024


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
    return {
        n for n in all_names if not is_rules_collection(n) and not is_campaign_lore_collection(n)
    }


def _unique_slug(desired: str, taken: set[str]) -> str:
    slug = desired
    i = 2
    while slug in taken:
        slug = f"{desired}_{i}"
        i += 1
    return slug


def _read_for_slug(slug: str, has_collection: bool) -> AdventureRead:
    meta = load_adventure_metadata(slug)
    from ..services.campaign_sources import source_read

    source = source_read(slug)
    status = source.status
    return AdventureRead(
        slug=slug,
        title=meta.get("title") or slug.replace("_", " ").title(),
        description=meta.get("description"),
        level_range=meta.get("level_range"),
        cover_image=meta.get("cover_image"),
        status=status,
        usable_chunks=source.usable_chunks,
        source_pdf_available=source.source_pdf_available,
        retrieval_degraded=status != "ready",
        chunks=meta.get("chunks"),
        source_filename=meta.get("source_filename"),
        error=source.message or meta.get("error"),
        created_at=meta.get("created_at"),
        failure_code=source.failure_code,
        extraction_preview_available=source.extraction_preview_available,
        extraction_preview_current=source.extraction_preview_current,
        ingestion=source.ingestion,
        can_retry=source.source_pdf_available or (meta.get("generation") or {}).get("kind") == "ai",
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


def _extraction_path(slug: str, name: str) -> Path:
    from ..services.campaign_sources import validate_binding

    validate_binding(slug)
    path = _ADVENTURES_DIR / slug / name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="No extraction preview is available yet.")
    return path


@router.get("/adventures/{slug}/extraction")
def get_extraction_preview(slug: str):
    """Local diagnostics and bounded representative text/stat/scene candidates."""
    try:
        data = _load_extraction(slug)
        from ingestion.quality import diagnostic_message

        readiness = data.get("readiness") or {
            "source_kind": "pdf",
            "extraction_sha256": data.get("markdown_sha256", ""),
            "required_pages": [p["page"] for p in data["pages"] if p.get("text", "").strip()],
            "chunk_count": 0,
            "quality_passed": False,
            "warning_count": sum(len(p.get("warnings", [])) for p in data["pages"]),
            "review_note": "This extraction preview has not been assessed for index readiness. Scene previews are unreviewed.",
            "issues": [
                {
                    "code": "readiness_not_assessed",
                    "message": "Inspect the extraction before indexing it.",
                }
            ],
            "previews": [
                {
                    "kind": "text",
                    "page": p["page"],
                    "page_end": p["page"],
                    "section": None,
                    "text": p["text"][:2000],
                }
                for p in data["pages"]
                if p.get("text", "").strip()
            ][:1],
        }
        return {
            **readiness,
            "source_sha256": data["source_sha256"],
            "page_count": data["page_count"],
            "source_current": _preview_source_current(slug, data),
            "pages": [
                {
                    **{key: value for key, value in page.items() if key != "text"},
                    "warning_messages": [diagnostic_message(w) for w in page.get("warnings", [])],
                }
                for page in data["pages"]
            ],
        }
    except (ValueError, OSError) as exc:
        raise HTTPException(
            status_code=409, detail="Extraction preview is unreadable. Retry extraction."
        ) from exc


@router.get("/adventures/{slug}/extraction/markdown")
def get_extraction_markdown(slug: str):
    current = _preview_source_current(slug, _load_extraction(slug))
    return FileResponse(
        _extraction_path(slug, f"{slug}.md"),
        media_type="text/plain; charset=utf-8",
        filename=f"{slug}-{'extraction' if current else 'previous-extraction'}.md",
        headers={"X-Extraction-Source-Current": str(current).lower()},
    )


def _load_extraction(slug: str) -> dict:
    path = _extraction_path(slug, f"{slug}.extraction.json")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("source_sha256"), str)
            or not isinstance(data.get("pages"), list)
            or not isinstance(data.get("page_count"), int)
        ):
            raise ValueError("Invalid extraction bundle")
        if not all(isinstance(page, dict) for page in data["pages"]):
            raise ValueError("Invalid extraction pages")
        return data
    except (ValueError, OSError) as exc:
        raise HTTPException(
            status_code=409, detail="Extraction preview is unreadable. Retry extraction."
        ) from exc


def _preview_source_current(slug: str, data: dict) -> bool:
    from ingestion.artifacts import source_digest

    readiness = data.get("readiness") or {}
    meta = load_adventure_metadata(slug)
    expected = (meta.get("ingestion") or {}).get("source_sha256")
    if expected and expected != data.get("source_sha256"):
        return False
    if readiness.get("source_kind", "pdf") == "pdf":
        try:
            return source_digest(_PDF_DIR / f"{slug}.pdf") == data.get("source_sha256")
        except OSError:
            return False
    return readiness.get("source_kind") == "generated"


def _ingest_adventure(
    slug: str,
    pdf_path: str,
    reviewed_extraction: str | None = None,
    reviewed_identity: tuple[str, str] | None = None,
) -> None:
    """Background worker: embed the PDF and flip the adventure's status to ready/failed."""
    # Imported here so the API process only pays the ingestion import cost on first upload.
    from ingestion.embed import run_pipeline
    from ingestion.readiness import IngestionRejected, report_summary

    def prepared(report: dict) -> None:
        meta = load_adventure_metadata(slug)
        meta.update({"ingestion": report_summary(report), "failure_code": None})
        _save_adventure_metadata(slug, meta)

    structlog.contextvars.bind_contextvars(adventure_slug=slug)
    try:
        n = run_pipeline(
            pdf_path=pdf_path,
            collection_name=slug,
            save_markdown=False,
            preview_dir=_ADVENTURES_DIR / slug,
            on_prepared=prepared,
            replace=reviewed_extraction is not None,
            reviewed_extraction=reviewed_extraction,
            reviewed_identity=reviewed_identity,
        )
        meta = load_adventure_metadata(slug)
        from ..services.campaign_sources import inspect_ingestion_source

        report = meta.get("ingestion") or {}
        coverage = inspect_ingestion_source(slug, {**meta, "chunks": n})
        ready = n > 0 and report.get("quality_passed") and coverage["status"] == "ready"
        meta.update(
            {
                "status": "ready" if ready else "failed",
                "chunks": n,
                "failure_code": None if ready else "index_coverage",
                "error": None
                if ready
                else "The index does not match the validated extraction. Inspect the preview and retry source recovery.",
            }
        )
        _save_adventure_metadata(slug, meta)
        log.info("adventure_ingested", slug=slug, chunks=n)
    except IngestionRejected as exc:
        meta = load_adventure_metadata(slug)
        meta.update(
            {
                "status": "failed",
                "chunks": 0,
                "failure_code": exc.code,
                "error": str(exc),
                "ingestion": report_summary(exc.report),
            }
        )
        _save_adventure_metadata(slug, meta)
        log.warning("adventure_quality_rejected", slug=slug, code=exc.code)
    except Exception as exc:
        log.exception("adventure_ingest_failed", slug=slug)
        meta = load_adventure_metadata(slug)
        # Raw exception text can carry internals; the full trace is in the logs.
        meta.update(
            {
                "status": "failed",
                "failure_code": "ingestion_error",
                "error": f"Ingestion failed ({type(exc).__name__}); see server logs.",
            }
        )
        _save_adventure_metadata(slug, meta)
    finally:
        structlog.contextvars.unbind_contextvars("adventure_slug")


def _copy_capped(src: BinaryIO, dest: Path, max_bytes: int) -> int:
    """Stream `src` to `dest` in chunks; return bytes written or -1 if over `max_bytes`.

    Runs in a worker thread. Writes to a temp file and renames, so a rejected or failed upload
    never leaves a partial PDF behind.
    """
    tmp = dest.with_suffix(dest.suffix + ".part")
    written = 0
    try:
        with tmp.open("wb") as out:
            while chunk := src.read(_COPY_CHUNK_BYTES):
                written += len(chunk)
                if written > max_bytes:
                    break
                out.write(chunk)
        if written > max_bytes:
            tmp.unlink(missing_ok=True)
            return -1
        tmp.replace(dest)
        return written
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _reserve_slug(desired: str, meta: dict) -> str:
    """Pick a free slug (no collection, no metadata file) and write its metadata. Blocking."""
    taken = set(_all_collection_names()) | metadata_slugs()
    while True:
        slug = _unique_slug(desired, taken)
        if reserve_meta(slug, meta):
            return slug
        taken.add(slug)


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

    desired_title = (title or Path(filename).stem.replace("_", " ").strip()) or "Untitled Adventure"
    # All blocking work (Qdrant HTTP call, metadata + PDF file IO) runs in the threadpool so
    # the event loop keeps serving other requests while a large upload is copied.
    slug = await run_in_threadpool(
        _reserve_slug,
        _slugify(desired_title),
        {
            "title": desired_title,
            "status": "processing",
            "chunks": None,
            "error": None,
            "source_filename": filename,
            "created_at": datetime.now(UTC).isoformat(),
        },
    )

    def _discard_reservation() -> None:
        _metadata_path(slug).unlink(missing_ok=True)

    await run_in_threadpool(_PDF_DIR.mkdir, parents=True, exist_ok=True)
    pdf_path = _PDF_DIR / f"{slug}.pdf"
    try:
        size = await run_in_threadpool(_copy_capped, file.file, pdf_path, _MAX_UPLOAD_BYTES)
    except Exception:
        await run_in_threadpool(_discard_reservation)
        raise
    if size <= 0:
        await run_in_threadpool(_discard_reservation)
        if size == 0:
            await run_in_threadpool(pdf_path.unlink, missing_ok=True)
            raise HTTPException(status_code=400, detail="Uploaded file is empty.")
        raise HTTPException(
            status_code=413,
            detail=f"PDF too large (max {_MAX_UPLOAD_BYTES // (1024 * 1024)} MB).",
        )

    background.add_task(_ingest_adventure, slug, str(pdf_path))
    log.info("adventure_upload_accepted", slug=slug, filename=filename, bytes=size)
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
    slug = await run_in_threadpool(
        _reserve_slug,
        base,
        {
            "title": title_hint or "AI campaign (generating…)",
            "status": "processing",
            "chunks": None,
            "error": None,
            "source_filename": None,
            "created_at": datetime.now(UTC).isoformat(),
            "generation": {
                "kind": "ai",
                "size": body.size,
                "theme": body.theme,
                "title_hint": title_hint,
            },
        },
    )
    background.add_task(build_generated_campaign, slug, title_hint, body.theme or "", body.size)
    log.info("adventure_generate_accepted", slug=slug, size=body.size)
    return AdventureUploadResponse(
        slug=slug, title=title_hint or "AI campaign", status="processing"
    )


@contextmanager
def _source_lock(slug: str):
    """Serialize source recovery reservations across API processes."""
    _ADVENTURES_DIR.mkdir(parents=True, exist_ok=True)
    with (_ADVENTURES_DIR / f"{slug}.recovery.lock").open("a") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise HTTPException(
                status_code=409, detail="Source recovery is already in progress."
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@router.post("/adventures/{slug}/retry", response_model=AdventureUploadResponse, status_code=202)
def retry_adventure(
    slug: str, background: BackgroundTasks, body: AdventureRetryRequest | None = None
):
    from ..services.campaign_sources import validate_binding

    validate_binding(slug)
    with _source_lock(slug):
        return _retry_adventure(slug, background, body)


def _retry_adventure(
    slug: str, background: BackgroundTasks, body: AdventureRetryRequest | None = None
):
    """Re-run ingestion (uploads) or generation (AI campaigns) for a failed adventure.

    Typical use: an adventure left "failed" because a restart interrupted its processing.
    409 if it is not missing/empty/partial/failed; 410 if there is nothing to retry from (the uploaded
    PDF is gone and it was not AI-generated).
    """
    meta = load_adventure_metadata(slug)
    if not meta:
        raise HTTPException(status_code=404, detail="Adventure not found")
    from ..services.campaign_sources import source_read

    if source_read(slug).status not in ("failed", "missing", "empty", "partial"):
        raise HTTPException(
            status_code=409, detail="Only unusable adventure sources can be retried."
        )

    title = meta.get("title") or slug.replace("_", " ").title()
    generation = meta.get("generation") or {}
    pdf_path = _PDF_DIR / f"{slug}.pdf"
    reviewed_identity = None
    if body is not None and (body.reviewed_source_sha256 or body.reviewed_extraction_sha256):
        if not body.reviewed_source_sha256 or not body.reviewed_extraction_sha256:
            raise HTTPException(
                status_code=422,
                detail="Both inspected source and extraction identities are required.",
            )
        if generation.get("kind") == "ai" or not pdf_path.is_file():
            raise HTTPException(
                status_code=409,
                detail="An inspected PDF preview is required for this recovery action.",
            )
        bundle = _load_extraction(slug)
        reviewed_identity = (body.reviewed_source_sha256, body.reviewed_extraction_sha256)
        if (
            not _preview_source_current(slug, bundle)
            or not (bundle.get("readiness") or {}).get("quality_passed")
            or (bundle.get("source_sha256"), bundle.get("markdown_sha256")) != reviewed_identity
        ):
            raise HTTPException(
                status_code=409,
                detail="The inspected extraction changed or failed quality checks. Inspect the current preview again.",
            )
    if generation.get("kind") == "ai":
        from ..services.campaign_generator import build_generated_campaign

        task_args: tuple = (
            build_generated_campaign,
            slug,
            generation.get("title_hint") or "",
            generation.get("theme") or "",
            generation.get("size") or "medium",
        )
    elif pdf_path.is_file():
        task_args = (
            (
                _ingest_adventure,
                slug,
                str(pdf_path),
                str(_extraction_path(slug, f"{slug}.extraction.json")),
                reviewed_identity,
            )
            if reviewed_identity is not None
            else (_ingest_adventure, slug, str(pdf_path))
        )
    else:
        raise HTTPException(status_code=410, detail="The source PDF is gone; upload it again.")

    meta.update({"status": "processing", "error": None, "chunks": None, "failure_code": None})
    _save_adventure_metadata(slug, meta)
    background.add_task(*task_args)
    log.info("adventure_retry_accepted", slug=slug)
    return AdventureUploadResponse(slug=slug, title=title, status="processing")


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
            log.error("adventure_delete_qdrant_failed", slug=slug, error=str(exc))
            raise HTTPException(
                status_code=502, detail="Vector store error while deleting the adventure."
            ) from exc

    meta_path = _metadata_path(slug)
    if meta_path.is_file():
        meta_path.unlink()

    pdf_path = _PDF_DIR / f"{slug}.pdf"
    if pdf_path.is_file():
        pdf_path.unlink()

    cover_dir = _ADVENTURES_DIR / slug
    if cover_dir.is_dir():
        shutil.rmtree(cover_dir, ignore_errors=True)


async def recover_source_upload(slug: str, file: UploadFile, background: BackgroundTasks):
    """Serialize recovery reservations and restore the intended slug, never a suffixed copy."""
    from ..services.campaign_sources import source_read, validate_binding

    validate_binding(slug)
    filename = file.filename or "adventure.pdf"
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported.")

    def accept():
        _ADVENTURES_DIR.mkdir(parents=True, exist_ok=True)
        # An OS lock prevents simultaneous uploads from copying to the same slug. The
        # processing metadata prevents a later request from starting a second ingest.
        with _source_lock(slug):
            source = source_read(slug)
            if source.status in ("ready", "processing", "unavailable"):
                raise HTTPException(
                    status_code=409,
                    detail="This source cannot be re-uploaded in its current state.",
                )
            _PDF_DIR.mkdir(parents=True, exist_ok=True)
            pdf_path = _PDF_DIR / f"{slug}.pdf"
            with tempfile.NamedTemporaryFile(dir=_PDF_DIR, suffix=".pdf", delete=False) as staged:
                staged_path = Path(staged.name)
            try:
                size = _copy_capped(file.file, staged_path, _MAX_UPLOAD_BYTES)
                if size <= 0:
                    raise HTTPException(
                        status_code=400 if size == 0 else 413,
                        detail="Uploaded PDF is empty or exceeds 64 MB.",
                    )
                staged_path.replace(pdf_path)
            finally:
                staged_path.unlink(missing_ok=True)
            meta = load_adventure_metadata(slug)
            meta.update(
                {
                    "title": source.title,
                    "status": "processing",
                    "chunks": None,
                    "error": None,
                    "failure_code": None,
                    "source_filename": filename,
                }
            )
            # A supplied PDF changes recovery provenance to upload, not AI generation.
            meta.pop("generation", None)
            _save_adventure_metadata(slug, meta)
            return pdf_path, source.title

    pdf_path, title = await run_in_threadpool(accept)
    background.add_task(_ingest_adventure, slug, str(pdf_path))
    return AdventureUploadResponse(slug=slug, title=title, status="processing")
