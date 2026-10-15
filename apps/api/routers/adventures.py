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
import uuid
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
from edgar_core.sources import SourceEdition

from ..schemas.adventure import (
    AdventureGenerateRequest,
    AdventureJobRead,
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
    new_job,
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
    job_read = source.job
    return AdventureRead(
        slug=slug,
        title=meta.get("title") or slug.replace("_", " ").title(),
        rules_edition=meta.get("rules_edition", "unspecified"),
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
        can_retry=(
            job_read.retryable
            if job_read is not None
            else source.source_pdf_available
            or (meta.get("generation") or {}).get("kind") == "ai"
        ),
        job=job_read,
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


def _extraction_path(
    slug: str, name: str, *, candidate_attempt_id: str | None = None
) -> Path:
    from ..services.campaign_sources import validate_binding

    validate_binding(slug)
    from ..services.adventures_meta import attempt_dir
    from ..services.campaign_sources import active_metadata

    raw_meta = load_adventure_metadata(slug)
    if candidate_attempt_id is None:
        job = raw_meta.get("job") or {}
        candidate_attempt_id = job.get("retained_source_attempt_id")
        # PDF recovery inspects the candidate before retry. Generated replacements keep
        # the accepted extraction visible while their private checkpoint is recoverable.
        if candidate_attempt_id is None and job.get("kind") == "ingestion":
            candidate_attempt_id = job.get("attempt_id")
    if isinstance(candidate_attempt_id, str):
        candidate = attempt_dir(slug, candidate_attempt_id) / name
        if candidate.is_file():
            return candidate
    meta = active_metadata(slug, raw_meta)
    token = meta.get("artifact_attempt")
    staged = attempt_dir(slug, token) / name if token else None
    path = staged if staged is not None and staged.is_file() else _ADVENTURES_DIR / slug / name
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
    from ..services.adventures_meta import candidate_pdf_path, source_pdf_path
    from ..services.campaign_sources import active_metadata

    if readiness.get("source_kind", "pdf") == "pdf":
        try:
            raw = load_adventure_metadata(slug)
            job = raw.get("job") or {}
            candidate_id = job.get("retained_source_attempt_id") or job.get("attempt_id")
            candidate = candidate_pdf_path(slug, candidate_id) if isinstance(candidate_id, str) else None
            if candidate is not None and candidate.is_file():
                return source_digest(candidate) == data.get("source_sha256")
            meta = active_metadata(slug, raw)
            expected = (meta.get("ingestion") or {}).get("source_sha256")
            if expected and expected != data.get("source_sha256"):
                return False
            return source_digest(source_pdf_path(slug, meta)) == data.get("source_sha256")
        except OSError:
            return False
    meta = active_metadata(slug)
    expected = (meta.get("ingestion") or {}).get("source_sha256")
    if expected and expected != data.get("source_sha256"):
        return False
    return readiness.get("source_kind") == "generated"


def _ingest_adventure(
    slug: str,
    pdf_path: str,
    reviewed_extraction: str | None = None,
    reviewed_identity: tuple[str, str] | None = None,
    *,
    attempt_id: str | None = None,
    source_filename: str | None = None,
) -> None:
    """Background worker: embed the PDF and flip the adventure's status to ready/failed."""
    # Imported here so the API process only pays the ingestion import cost on first upload.
    from db.vector.versions import SupersededIngestion
    from ingestion.embed import run_pipeline
    from ingestion.readiness import IngestionRejected, report_summary

    from ..services import adventures_meta as store

    token = attempt_id or store.begin_attempt(slug)
    report = {}
    store.ensure_job(slug, token, kind="ingestion", stage="extracting")
    publication_attempt = 0

    def prepared(value: dict) -> None:
        report.update(value)
        store.update_job(
            slug,
            token,
            retained_work=("inspected_extraction" if reviewed_extraction else "source_pdf"),
            source_version=value.get("index_version"),
        )
        store.prepare_attempt(slug, token, value)

    def publication_guard():
        nonlocal publication_attempt
        publication_attempt += 1
        return store.publication_guard(
            slug,
            token,
            stage="embedding" if publication_attempt == 1 else "publishing",
        )

    structlog.contextvars.bind_contextvars(adventure_slug=slug)
    try:
        store.check_job_running(slug, token)
        n = run_pipeline(
            pdf_path=pdf_path,
            collection_name=slug,
            save_markdown=False,
            preview_dir=store.attempt_dir(slug, token),
            on_prepared=prepared,
            replace=reviewed_extraction is not None,
            reviewed_extraction=reviewed_extraction,
            reviewed_identity=reviewed_identity,
            rules_edition=load_adventure_metadata(slug).get("rules_edition", "unspecified"),
            publication_guard=publication_guard,
        )
        meta = load_adventure_metadata(slug)
        from ..services.campaign_sources import inspect_ingestion_source

        coverage_meta = {**meta, "chunks": n, "ingestion": report_summary(report)}
        if store.candidate_pdf_path(slug, token).is_file():
            # Validate the activated candidate against its immutable PDF. The canonical
            # PDF still belongs to the previous source until successful finalization.
            coverage_meta["source_pdf_attempt_id"] = token
        coverage = inspect_ingestion_source(slug, coverage_meta)
        ready = n > 0 and report.get("quality_passed") and coverage["status"] == "ready"
        store.finish_attempt(
            slug,
            token,
            {
                "status": "ready" if ready else "failed",
                "chunks": n,
                "failure_code": None if ready else "index_coverage",
                "error": None
                if ready
                else "The index does not match the validated extraction. Inspect the preview and retry source recovery.",
                **({"source_filename": source_filename} if source_filename else {}),
            },
            success=bool(ready),
        )
        log.info("adventure_ingested", slug=slug, chunks=n)
    except store.JobCancelled:
        store.cancel_attempt(slug, token)
        log.info("adventure_ingest_cancelled", slug=slug)
    except SupersededIngestion:
        log.info("adventure_ingest_superseded", slug=slug)
    except IngestionRejected as exc:
        store.finish_attempt(
            slug,
            token,
            {
                "status": "failed",
                "chunks": 0,
                "failure_code": exc.code,
                "error": str(exc),
                "ingestion": report_summary(exc.report),
            },
        )
        log.warning("adventure_quality_rejected", slug=slug, code=exc.code)
    except Exception as exc:
        log.exception("adventure_ingest_failed", slug=slug)
        # Raw exception text can carry internals; the full trace is in the logs.
        store.finish_attempt(
            slug,
            token,
            {
                "status": "failed",
                "failure_code": "ingestion_error",
                "error": f"Ingestion failed ({type(exc).__name__}); see server logs.",
            },
        )
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
    rules_edition: SourceEdition = Form(default="unspecified"),
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
            "rules_edition": rules_edition,
            "status": "processing",
            "chunks": None,
            "error": None,
            "source_filename": filename,
            "created_at": datetime.now(UTC).isoformat(),
            "job": new_job("ingestion", "queued", retained_work="source_pdf"),
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

    from ..services.adventures_meta import begin_attempt

    token = begin_attempt(slug, job_attempt=True, job_kind="ingestion")
    background.add_task(_ingest_adventure, slug, str(pdf_path), attempt_id=token)
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
            "rules_edition": body.rules_edition,
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
            "job": new_job(
                "generation",
                "queued",
                total_chapters={"small": 3, "medium": 6, "large": 12, "gigantic": 24}.get(
                    body.size, 6
                ),
            ),
        },
    )
    from ..services.adventures_meta import begin_attempt

    token = begin_attempt(slug, job_attempt=True, job_kind="generation")
    background.add_task(
        build_generated_campaign, slug, title_hint, body.theme or "", body.size, attempt_id=token
    )
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

    job = meta.get("job") if isinstance(meta.get("job"), dict) else {}
    recoverable_job = bool(job.get("retryable")) and job.get("state") in (
        "interrupted", "failed", "cancelled"
    )
    if meta.get("status") == "processing" or (
        source_read(slug).status not in ("failed", "missing", "empty", "partial")
        and (meta.get("last_attempt") or {}).get("status") != "failed"
        and not recoverable_job
    ):
        raise HTTPException(
            status_code=409, detail="Only unusable adventure sources can be retried."
        )

    title = meta.get("title") or slug.replace("_", " ").title()
    generation = meta.get("generation") or {}
    retained_candidate = job.get("retained_source_attempt_id")
    retained_candidate_path = (
        _ADVENTURES_DIR / slug / ".attempts" / retained_candidate / f"{slug}.pdf"
        if isinstance(retained_candidate, str)
        else None
    )
    pdf_path = _PDF_DIR / f"{slug}.pdf"
    reviewed_identity = None
    if body is not None and (body.reviewed_source_sha256 or body.reviewed_extraction_sha256):
        if not body.reviewed_source_sha256 or not body.reviewed_extraction_sha256:
            raise HTTPException(
                status_code=422,
                detail="Both inspected source and extraction identities are required.",
            )
        if generation.get("kind") == "ai" or not (
            pdf_path.is_file()
            or (retained_candidate_path is not None and retained_candidate_path.is_file())
        ):
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
    if job.get("checkpoint_corrupt"):
        raise HTTPException(
            status_code=409,
            detail="Saved generation progress is unreadable. Create a new campaign to generate again.",
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
    elif retained_candidate_path and retained_candidate_path.is_file():
        task_args = (
            _ingest_adventure,
            slug,
            str(retained_candidate_path),
            *(
                (
                    str(
                        _extraction_path(
                            slug,
                            f"{slug}.extraction.json",
                            candidate_attempt_id=retained_candidate,
                        )
                    ),
                    reviewed_identity,
                )
                if reviewed_identity is not None
                else ()
            ),
        )
    elif pdf_path.is_file():
        task_args = (
            _ingest_adventure,
            slug,
            str(pdf_path),
            *(
                (str(_extraction_path(slug, f"{slug}.extraction.json")), reviewed_identity)
                if reviewed_identity is not None
                else ()
            ),
        )
    else:
        raise HTTPException(status_code=410, detail="The source PDF is gone; upload it again.")

    from ..services.adventures_meta import begin_attempt

    token = begin_attempt(
        slug,
        job_attempt=True,
        job_kind="generation" if generation.get("kind") == "ai" else "ingestion",
    )
    if retained_candidate_path and retained_candidate_path.is_file() and generation.get("kind") != "ai":
        from ..services.adventures_meta import stage_attempt_pdf

        candidate_path = stage_attempt_pdf(
            slug,
            token,
            retained_candidate_path,
            job.get("retained_source_filename"),
        )
        task_args = (
            _ingest_adventure,
            slug,
            str(candidate_path),
            *(
                (
                    str(
                        _extraction_path(
                            slug,
                            f"{slug}.extraction.json",
                            candidate_attempt_id=retained_candidate,
                        )
                    ),
                    reviewed_identity,
                )
                if reviewed_identity is not None
                else ()
            ),
        )
    background.add_task(*task_args, attempt_id=token)
    log.info("adventure_retry_accepted", slug=slug)
    return AdventureUploadResponse(slug=slug, title=title, status="processing")


@router.post("/adventures/{slug}/cancel", response_model=AdventureJobRead)
def cancel_adventure(slug: str):
    from ..services.adventures_meta import request_job_cancel
    from ..services.campaign_sources import safe_job_read, validate_binding

    validate_binding(slug)
    with _source_lock(slug):
        try:
            request_job_cancel(slug)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    return safe_job_read(load_adventure_metadata(slug), slug)


@router.patch("/adventures/{slug}", response_model=AdventureRead)
def update_adventure(slug: str, body: AdventureUpdate):
    """Update metadata; changing the edition invalidates any earlier manifest receipt."""
    all_names = _all_collection_names()
    collection_slugs = _adventure_collection_names(all_names)
    if slug not in collection_slugs and not _metadata_path(slug).is_file():
        raise HTTPException(status_code=404, detail="Adventure not found")
    from ..services.adventures_meta import lifecycle_lock

    with lifecycle_lock(slug):
        meta = load_adventure_metadata(slug)
        if meta.get("status") == "processing":
            raise HTTPException(
                status_code=409, detail="Wait for source processing before editing its metadata."
            )
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
            if load_adventure_metadata(slug).get("status") == "processing" or source.status in (
                "ready",
                "processing",
                "unavailable",
            ):
                raise HTTPException(
                    status_code=409,
                    detail="This source cannot be re-uploaded in its current state.",
                )
            upload_dir = _ADVENTURES_DIR / slug / ".attempts" / f"upload-{uuid.uuid4().hex}"
            upload_dir.mkdir(parents=True, exist_ok=True)
            staged_path = upload_dir / "source.pdf"
            try:
                size = _copy_capped(file.file, staged_path, _MAX_UPLOAD_BYTES)
                if size <= 0:
                    raise HTTPException(
                        status_code=400 if size == 0 else 413,
                        detail="Uploaded PDF is empty or exceeds 64 MB.",
                    )
                from ..services.adventures_meta import begin_attempt, stage_attempt_pdf

                token = begin_attempt(
                    slug, job_attempt=True, job_kind="ingestion", reset_job=True
                )
                candidate = stage_attempt_pdf(slug, token, staged_path, filename)
                meta = load_adventure_metadata(slug)
                meta.update(title=source.title, chunks=None, error=None, failure_code=None)
                # A supplied PDF changes recovery provenance to upload, not AI generation.
                meta.pop("generation", None)
                _save_adventure_metadata(slug, meta)
                return candidate, source.title, token
            except BaseException:
                shutil.rmtree(upload_dir, ignore_errors=True)
                raise
            finally:
                # The durable attempt copy is private to the job; discard upload staging.
                shutil.rmtree(upload_dir, ignore_errors=True)

    candidate_path, title, token = await run_in_threadpool(accept)
    background.add_task(
        _ingest_adventure,
        slug,
        str(candidate_path),
        attempt_id=token,
        source_filename=filename,
    )
    return AdventureUploadResponse(slug=slug, title=title, status="processing")
