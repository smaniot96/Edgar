"""Adventures registry: upload + embed campaign PDFs, list installed modules, manage metadata.

An "adventure" is an embedded campaign module: a PDF that has been chunked, embedded, and
upserted into its own Qdrant collection (the slug). Players then start a Campaign from a ready
adventure (see routers/campaign.py). Uploads embed in the background; the metadata JSON beside
the PDF store tracks the lifecycle (processing -> ready | failed) so it survives DB resets.
"""

import fcntl
import functools
import json
import re
import shutil
from collections.abc import AsyncIterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, BinaryIO

import structlog
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import FileResponse
from qdrant_client.http.exceptions import UnexpectedResponse
from redis.asyncio import Redis
from starlette.concurrency import run_in_threadpool

from db.vector.client import get_qdrant_client
from db.vector.collections import (
    is_campaign_lore_collection,
    is_rules_collection,
)
from edgar_core import config as limit_config
from edgar_core.sources import SourceEdition

from ..auth import Auth, require_admin
from ..dependencies import get_redis
from ..limits import (
    SCOPE_UPLOAD_SIZE,
    Admission,
    admit_job,
    declared_budget_microusd,
    job_budget_microusd,
    reject,
    require_pdf_upload,
    schedule_job,
)
from ..schemas.adventure import (
    AdventureGenerateRequest,
    AdventureJobRead,
    AdventurePreparationRequest,
    AdventureRead,
    AdventureRetryRequest,
    AdventureUpdate,
    AdventureUploadResponse,
)
from ..schemas.campaign import SourceReleaseOutcome
from ..services.adventures_meta import (
    ADVENTURES_DIR as _ADVENTURES_DIR,
    PDF_DIR as _PDF_DIR,
    adventure_path as _adventure_path,
    canonical_pdf_path as _canonical_pdf_path,
    load_meta as load_adventure_metadata,
    meta_path as _metadata_path,
    metadata_slugs,
    new_job,
    recovery_lock_path as _recovery_lock_path,
    reserve_meta,
    save_meta as _save_adventure_metadata,
)
from ..services.campaign_sources import is_private_preparation_source
from ..services.safe_errors import (
    classify_error,
    classify_http_failure,
    log_failure,
)

log = structlog.get_logger()
router = APIRouter(tags=["adventures"])

_MAX_UPLOAD_BYTES = limit_config.LIMIT_MAX_UPLOAD_BYTES  # EDG-066: set by LIMIT_MAX_UPLOAD_BYTES
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
        failure = classify_error(exc)
        log_failure("qdrant_unavailable", failure)
        raise HTTPException(
            status_code=503,
            detail="Vector store unavailable; cannot list adventures.",
        ) from exc


def _adventure_collection_names(all_names: list[str]) -> set[str]:
    from ..services.adventures_meta import valid_adventure_slug

    return {
        n
        for n in all_names
        if valid_adventure_slug(n)
        and not is_rules_collection(n)
        and not is_campaign_lore_collection(n)
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
    from ..services.campaign_prep.runtime import preparation_setup_needed
    from ..services.cover_images import normalize_cover_image

    try:
        cover_image = normalize_cover_image(meta.get("cover_image"))
    except ValueError:
        # Old metadata may predate the local-path contract. An invalid optional cover should
        # degrade to the card's normal fallback instead of breaking the adventure listing.
        cover_image = None
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
        cover_image=cover_image,
        status=status,
        usable_chunks=source.usable_chunks,
        source_pdf_available=source.source_pdf_available,
        retrieval_degraded=status != "ready",
        chunks=meta.get("chunks"),
        source_filename=meta.get("source_filename"),
        error=source.message,
        created_at=meta.get("created_at"),
        failure_code=source.failure_code,
        extraction_preview_available=source.extraction_preview_available,
        extraction_preview_current=source.extraction_preview_current,
        ingestion=source.ingestion,
        can_retry=(
            job_read.retryable
            if job_read is not None
            else source.source_pdf_available or (meta.get("generation") or {}).get("kind") == "ai"
        ),
        job=job_read,
        preparation_setup_required=preparation_setup_needed(meta),
    )


def _list_adventures():
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


def _get_adventure(slug: str):
    from ..services.campaign_sources import validate_binding

    validate_binding(slug)
    all_names = _all_collection_names()
    collection_slugs = _adventure_collection_names(all_names)
    if slug not in collection_slugs and not _metadata_path(slug).is_file():
        raise HTTPException(status_code=404, detail="Adventure not found")
    return _read_for_slug(slug, has_collection=slug in collection_slugs)


async def _with_readiness(adventure: AdventureRead) -> AdventureRead:
    """Use actual play gates for modules before a campaign has been created."""
    from db.postgres.models import Campaign

    from ..services.campaign_readiness import evaluate_play_readiness

    prospective = Campaign(
        id=0,
        status="active",
        source_mode="module",
        adventure_collections=[adventure.slug],
        rules_edition=adventure.rules_edition,
    )
    adventure.readiness = await evaluate_play_readiness(
        None, prospective, observed_sources=[adventure]
    )
    return adventure


@router.get("/adventures", response_model=list[AdventureRead])
async def list_adventures(auth: Auth):
    adventures = await run_in_threadpool(_list_adventures)
    if auth is not None and not auth.is_admin:
        # In-progress private preparations belong to the operator until they are published.
        adventures = [
            a
            for a in adventures
            if not await run_in_threadpool(is_private_preparation_source, a.slug)
        ]
    return [await _with_readiness(adventure) for adventure in adventures]


@router.get("/adventures/{slug}", response_model=AdventureRead)
async def get_adventure(slug: str, auth: Auth):
    if (
        auth is not None
        and not auth.is_admin
        and await run_in_threadpool(is_private_preparation_source, slug)
    ):
        raise HTTPException(status_code=404, detail="Adventure not found")
    return await _with_readiness(await run_in_threadpool(_get_adventure, slug))


def _extraction_path(slug: str, name: str, *, candidate_attempt_id: str | None = None) -> Path:
    from ..services.campaign_sources import validate_binding

    validate_binding(slug)
    raw_meta = load_adventure_metadata(slug)
    if raw_meta.get("private_preparation") is True:
        from ..services.campaign_prep.runtime import failed_source_preview_dir

        # A private source exposes only the extraction of a PDF that failed before any
        # preparation run existed; otherwise it answers like an unknown source.
        directory = failed_source_preview_dir(slug, raw_meta)
        if directory is None or not (directory / name).is_file():
            raise HTTPException(status_code=404, detail="Not found")
        return directory / name
    from ..services.adventures_meta import attempt_dir
    from ..services.campaign_sources import active_metadata

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
    path = staged if staged is not None and staged.is_file() else _adventure_path(slug, name)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="No extraction preview is available yet.")
    return path


@router.get("/adventures/{slug}/extraction", dependencies=[Depends(require_admin)])
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


@router.get("/adventures/{slug}/extraction/markdown", dependencies=[Depends(require_admin)])
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
            candidate = (
                candidate_pdf_path(slug, candidate_id) if isinstance(candidate_id, str) else None
            )
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


def _ingest_adventure(slug: str, pdf_path: str, *, attempt_id: str | None = None) -> None:
    """PDF uploads enter the same new graph as AI campaigns."""
    import asyncio

    from ..services.campaign_prep.runtime import prepare_adventure

    asyncio.run(prepare_adventure(slug, pdf_path=pdf_path, attempt_token=attempt_id))


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


def _preparation_spend(slug: str):
    """Settle a private preparation job's daily-limit reservation against its own ledger."""
    from ..services.campaign_prep.runtime import preparation_job_spend

    return functools.partial(preparation_job_spend, slug)


def _reserve_slug(desired: str, meta: dict) -> str:
    """Pick a free slug (no collection, no metadata file) and write its metadata. Blocking."""
    taken = set(_all_collection_names()) | metadata_slugs()
    while True:
        slug = _unique_slug(desired, taken)
        if reserve_meta(slug, meta):
            return slug
        taken.add(slug)


@router.post(
    "/adventures/upload",
    response_model=AdventureUploadResponse,
    status_code=202,
    dependencies=[Depends(require_admin)],
)
async def upload_adventure(
    request: Request,
    background: BackgroundTasks,
    file: UploadFile = File(...),
    title: str | None = Form(default=None),
    rules_edition: SourceEdition = Form(default="unspecified"),
    preparation: str | None = Form(default=None, max_length=16000),
    redis: Redis = Depends(get_redis),
):
    """Upload a campaign PDF; it embeds in the background and becomes a playable adventure.

    Returns 202 immediately with status "processing"; poll GET /adventures for the slug to see
    it flip to "ready" (or "failed"). Rate and job-concurrency limits apply (EDG-066).
    """
    admission = await admit_job(request, redis)
    try:
        return await _accept_upload(background, file, title, rules_edition, preparation, admission)
    finally:
        await admission.settle()


async def _accept_upload(
    background: BackgroundTasks,
    file: UploadFile,
    title: str | None,
    rules_edition: SourceEdition,
    preparation: str | None,
    admission: Admission,
) -> AdventureUploadResponse:
    from ..schemas.preparation_setup import PreparationSetup
    from ..services.campaign_prep.runtime import capture_setup

    try:
        setup = PreparationSetup.model_validate_json(preparation) if preparation else None
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail="Provide valid preparation preferences."
        ) from exc
    captured = await capture_setup(setup)
    filename = file.filename or "adventure.pdf"
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported.")
    await require_pdf_upload(file)
    await admission.reserve_daily_spend(job_budget_microusd(None))

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
            "private_preparation": True,
            "preparation_workflow": "campaign-graph-2.0",
            "preparation_setup": captured,
            "created_at": datetime.now(UTC).isoformat(),
            "job": new_job("ingestion", "queued", retained_work="source_pdf"),
        },
    )

    def _discard_reservation() -> None:
        _metadata_path(slug).unlink(missing_ok=True)

    await run_in_threadpool(_PDF_DIR.mkdir, parents=True, exist_ok=True)
    pdf_path = _canonical_pdf_path(slug)
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
        raise reject(SCOPE_UPLOAD_SIZE, "upload_too_large")

    from ..services.adventures_meta import begin_attempt

    token = begin_attempt(slug, job_attempt=True, job_kind="ingestion")
    schedule_job(
        background,
        admission,
        _ingest_adventure,
        slug,
        str(pdf_path),
        attempt_id=token,
        spend_probe=_preparation_spend(slug),
    )
    log.info("adventure_upload_accepted", slug=slug, filename=filename, bytes=size)
    return AdventureUploadResponse(slug=slug, title=desired_title, status="processing")


@router.post(
    "/adventures/generate",
    response_model=AdventureUploadResponse,
    status_code=202,
    dependencies=[Depends(require_admin)],
)
async def generate_adventure(
    body: AdventureGenerateRequest,
    request: Request,
    background: BackgroundTasks,
    redis: Redis = Depends(get_redis),
):
    """Prepare a reusable AI-authored campaign in the background.

    `size` (small | medium | large | gigantic) scales the requested adventure scope.
    A shared campaign spine and chapter bundles are assembled into playable records. Returns 202 with
    status "processing"; poll GET /adventures for the slug. The declared preparation
    budget is an advisory usage estimate recorded before work starts.
    """
    admission = await admit_job(request, redis)
    try:
        return await _accept_generation(body, background, admission)
    finally:
        await admission.settle()


async def _accept_generation(
    body: AdventureGenerateRequest, background: BackgroundTasks, admission: Admission
) -> AdventureUploadResponse:
    from ..services import generation_requests
    from ..services.campaign_prep.runtime import capture_setup, prepare_adventure

    identity = generation_requests.identity_for(body)
    if identity is not None:
        # Replay before capturing setup, so a retry retains the original module request.
        replay = await run_in_threadpool(generation_requests.find_existing, identity)
        if replay is not None:
            return _generate_response(*replay)
    captured = await capture_setup(body.preparation)
    await admission.reserve_daily_spend(job_budget_microusd(declared_budget_microusd(captured)))
    title_hint = (body.title or "").strip()
    base = _slugify(title_hint or body.theme or "ai campaign")
    meta = {
        "title": title_hint or "AI campaign (generating…)",
        "rules_edition": body.rules_edition,
        "status": "processing",
        "chunks": None,
        "error": None,
        "source_filename": None,
        "private_preparation": True,
        "preparation_workflow": "campaign-graph-2.0",
        "preparation_setup": captured,
        "created_at": datetime.now(UTC).isoformat(),
        "generation": {
            "kind": "ai",
            "size": body.size,
            "theme": body.theme,
            "title_hint": title_hint,
        },
        "job": new_job("generation", "queued"),
    }
    slug, stored, created = await run_in_threadpool(
        generation_requests.claim, identity, base, meta, _reserve_slug
    )
    if not created:
        # Same identity and body as an earlier accept: no second module and no second dispatch.
        return _generate_response(slug, stored)
    from ..services.adventures_meta import begin_attempt

    token = begin_attempt(slug, job_attempt=True, job_kind="generation")
    schedule_job(
        background,
        admission,
        prepare_adventure,
        slug,
        attempt_token=token,
        spend_probe=_preparation_spend(slug),
    )
    log.info("adventure_generate_accepted", slug=slug, size=body.size)
    return _generate_response(slug, stored)


def _generate_response(slug: str, meta: dict) -> AdventureUploadResponse:
    """Accepted-generation response; a replay reports the stored state of the same module."""
    title_hint = (meta.get("generation") or {}).get("title_hint") or ""
    return AdventureUploadResponse(
        slug=slug, title=title_hint or "AI campaign", status=meta.get("status") or "processing"
    )


@contextmanager
def _source_lock(slug: str):
    """Serialize source recovery reservations across API processes."""
    _ADVENTURES_DIR.mkdir(parents=True, exist_ok=True)
    with _recovery_lock_path(slug).open("a") as handle:
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


async def admit_retry_job(
    slug: str,
    request: Request,
    redis: Redis = Depends(get_redis),
) -> AsyncIterator[Admission]:
    """Admit a retry before any work: per-client starts and a job lease.

    The retry route is synchronous, so admission runs here on the event loop. Whatever the route
    did not hand to a background job is undone when the request ends (EDG-066).
    """
    admission = await admit_job(request, redis)
    try:
        # The original job already recorded its campaign estimate. Resuming its
        # checkpoint must not record that entire estimate again. A zero estimate
        # retains the accounting key for incremental usage settlement.
        await admission.reserve_daily_spend(0)
        yield admission
    finally:
        await admission.settle()


@router.post(
    "/adventures/{slug}/retry",
    response_model=AdventureUploadResponse,
    status_code=202,
    dependencies=[Depends(require_admin)],
)
def retry_adventure(
    slug: str,
    background: BackgroundTasks,
    body: AdventureRetryRequest | None = None,
    admission: Annotated[Admission | None, Depends(admit_retry_job)] = None,
):
    from ..services.campaign_sources import validate_binding

    validate_binding(slug)
    with _source_lock(slug):
        return _retry_adventure(slug, background, body, admission)


@router.post(
    "/adventures/{slug}/preparation/setup",
    response_model=AdventureUploadResponse,
    status_code=202,
    dependencies=[Depends(require_admin)],
)
async def setup_preparation(
    slug: str,
    body: AdventurePreparationRequest,
    background: BackgroundTasks,
    request: Request,
    redis: Redis = Depends(get_redis),
):
    """Complete only missing non-spoiler setup before the first frozen run exists."""
    admission = await admit_job(request, redis)
    try:
        return await _setup_preparation(slug, body, background, admission)
    finally:
        await admission.settle()


async def _setup_preparation(
    slug: str, body: AdventurePreparationRequest, background: BackgroundTasks, admission: Admission
) -> AdventureUploadResponse:
    from ..services import adventures_meta as store
    from ..services.campaign_prep.runtime import (
        capture_setup,
        discard_unbound_source,
        prepare_adventure,
    )
    from ..services.campaign_sources import validate_binding

    validate_binding(slug)
    captured = await capture_setup(body.preparation)
    await admission.reserve_daily_spend(job_budget_microusd(declared_budget_microusd(captured)))
    with _source_lock(slug):
        with store.lifecycle_lock(slug):
            meta = store.load_meta(slug)
            if meta.get("private_preparation") is not True:
                raise HTTPException(status_code=404, detail="Adventure not found")
            if meta.get("preparation_run_id") or meta.get("prepared_bundle"):
                raise HTTPException(
                    status_code=409,
                    detail="This preparation is already bound. Create a new adventure to change its setup.",
                )
            if meta.get("preparation_setup") and meta.get("status") == "processing":
                raise HTTPException(status_code=409, detail="Preparation is already starting.")
            # A new setup after a pre-run failure reads the source again, not a stale extraction.
            discard_unbound_source(slug, meta)
            meta.update(preparation_setup=captured, rules_edition=body.rules_edition)
            meta.pop("preparation_blocked_code", None)
            store.save_meta(slug, meta)
        token = store.begin_attempt(
            slug,
            job_attempt=True,
            job_kind="generation"
            if (meta.get("generation") or {}).get("kind") == "ai"
            else "ingestion",
        )
    schedule_job(
        background,
        admission,
        prepare_adventure,
        slug,
        attempt_token=token,
        spend_probe=_preparation_spend(slug),
    )
    return AdventureUploadResponse(
        slug=slug, title=meta.get("title") or "Adventure", status="processing"
    )


def _retry_adventure(
    slug: str,
    background: BackgroundTasks,
    body: AdventureRetryRequest | None = None,
    admission: Admission | None = None,
):
    """Resume private preparation or reindex a failed public PDF source.

    Typical use: an adventure left "failed" because a restart interrupted its processing.
    409 if it is not missing/empty/partial/failed; 410 if there is nothing to retry from (the uploaded
    PDF is gone).
    """
    meta = load_adventure_metadata(slug)
    if not meta:
        raise HTTPException(status_code=404, detail="Adventure not found")
    if meta.get("private_preparation") is True:
        from ..services.campaign_prep.runtime import prepare_adventure, retry_preparation
        from ..services.campaign_prep.store import NeedsAttention as PreparationError

        try:
            _, token = retry_preparation(slug)
        except (PreparationError, ValueError) as exc:
            raise HTTPException(
                status_code=409,
                detail="Saved preparation cannot be retried. Check its setup or start a new adventure.",
            ) from exc
        schedule_job(
            background,
            admission,
            prepare_adventure,
            slug,
            attempt_token=token,
            spend_probe=_preparation_spend(slug),
        )
        return AdventureUploadResponse(
            slug=slug, title=meta.get("title") or "Adventure", status="processing"
        )
    raise HTTPException(
        status_code=409,
        detail="Archived source work is held. Upload a fresh PDF or create a new campaign to use the current workflow.",
    )


@router.post(
    "/adventures/{slug}/cancel",
    response_model=AdventureJobRead,
    dependencies=[Depends(require_admin)],
)
def cancel_adventure(slug: str):
    from ..services.adventures_meta import request_job_cancel
    from ..services.campaign_sources import safe_job_read, validate_binding

    validate_binding(slug)
    with _source_lock(slug):
        try:
            if load_adventure_metadata(slug).get("private_preparation") is True:
                from ..services.campaign_prep.runtime import cancel_preparation

                cancel_preparation(slug)
            else:
                request_job_cancel(slug)
        except ValueError as exc:
            failure = classify_http_failure(409, code="preparation_busy")
            raise HTTPException(status_code=409, detail=failure.to_payload()) from exc
    return safe_job_read(load_adventure_metadata(slug), slug)


@router.patch(
    "/adventures/{slug}", response_model=AdventureRead, dependencies=[Depends(require_admin)]
)
def update_adventure(slug: str, body: AdventureUpdate):
    """Update metadata; changing the edition invalidates any earlier manifest receipt."""
    from ..services.campaign_sources import validate_binding

    validate_binding(slug)
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
        from ..services.preparation_privacy import require_mutable_source

        require_mutable_source(meta)
        meta.update(body.model_dump(exclude_unset=True))
        _save_adventure_metadata(slug, meta)
    return _read_for_slug(slug, has_collection=slug in collection_slugs)


@router.delete("/adventures/{slug}", status_code=204, dependencies=[Depends(require_admin)])
def delete_adventure(slug: str, x_confirm_delete: str | None = Header(default=None)):
    """Drop the Qdrant collection, its metadata, the uploaded PDF, and any cover dir.

    Requires `X-Confirm-Delete: yes` header to prevent accidental deletions.
    """
    if x_confirm_delete != "yes":
        raise HTTPException(
            status_code=400,
            detail="Send header X-Confirm-Delete: yes to confirm deletion.",
        )
    from ..services.campaign_sources import validate_binding

    validate_binding(slug)
    all_names = _all_collection_names()
    collection_slugs = _adventure_collection_names(all_names)
    if slug not in collection_slugs and not _metadata_path(slug).is_file():
        raise HTTPException(status_code=404, detail="Adventure not found")

    from ..services.preparation_privacy import require_mutable_source

    require_mutable_source(load_adventure_metadata(slug))
    remove_adventure_source(slug, has_collection=slug in collection_slugs)


def remove_adventure_source(slug: str, *, has_collection: bool) -> None:
    """Remove only an unprepared source; retained work is never recursive cleanup."""
    from ..services.campaign_sources import validate_binding

    validate_binding(slug)
    meta = load_adventure_metadata(slug)
    job = meta.get("job") if isinstance(meta.get("job"), dict) else {}
    if meta.get("status") == "processing" or job.get("state") in _ACTIVE_JOB_STATES:
        raise HTTPException(
            status_code=409, detail="Wait for source processing before deleting its source."
        )
    if _has_retained_source_work(slug, meta):
        raise HTTPException(
            status_code=409,
            detail="Saved source artifacts and preparation receipts are retained. "
            "Deleting a campaign does not discard this work.",
        )
    if has_collection:
        client = get_qdrant_client()
        try:
            client.delete_collection(slug)
        except UnexpectedResponse as exc:
            failure = classify_error(exc)
            log_failure("adventure_delete_qdrant_failed", failure)
            raise HTTPException(
                status_code=502, detail="Vector store error while deleting the adventure."
            ) from exc

    meta_path = _metadata_path(slug)
    if meta_path.is_file():
        meta_path.unlink()

    pdf_path = _canonical_pdf_path(slug)
    if pdf_path.is_file():
        pdf_path.unlink()

    cover_dir = _adventure_path(slug)
    if cover_dir.is_dir():
        shutil.rmtree(cover_dir, ignore_errors=True)


def _has_retained_source_work(slug: str, meta: dict) -> bool:
    """Keep paid/archived work even when publication failed or metadata is incomplete.

    Prepared-bundle presence is not an ownership or retention boundary: held, cancelled,
    exhausted and interrupted preparations can already contain paid provider receipts.
    Unknown files are retained as well; automatic campaign cleanup cannot establish that
    an arbitrary artifact directory is disposable.
    """
    if meta.get("private_preparation") is True or any(
        key in meta
        for key in (
            "preparation_workflow",
            "preparation_run_id",
            "preparation_publication_receipt",
            "prepared_bundle",
            "generation",
            "index_receipts",
            "ready_receipt",
        )
    ):
        return True
    job = meta.get("job") if isinstance(meta.get("job"), dict) else {}
    if (
        job.get("kind") in {"generation", "ingestion"}
        or job.get("checkpoint_available")
        or job.get("retained_work") not in (None, "none")
    ):
        return True
    # load_meta fails closed to {} on malformed JSON; do not turn that into deletion consent.
    if _metadata_path(slug).is_file() and not meta:
        return True
    directory = _adventure_path(slug)
    return directory.is_dir() and next(directory.iterdir(), None) is not None


def release_unused_source(slug: str) -> SourceReleaseOutcome:
    """Remove a source after its last campaign is deleted, unless it must stay.

    The caller has already checked that no campaign binds the slug. A source that is still
    being prepared, or a prepared version retained for saved games, is kept.
    """
    from ..services.adventures_meta import lifecycle_lock
    from ..services.campaign_sources import validate_binding
    from ..services.preparation_privacy import require_mutable_source

    try:
        validate_binding(slug)
    except HTTPException:
        return "protected"
    with lifecycle_lock(slug):
        meta = load_adventure_metadata(slug)
        job = meta.get("job") if isinstance(meta.get("job"), dict) else {}
        if meta.get("status") == "processing" or job.get("state") in _ACTIVE_JOB_STATES:
            return "processing"
        if _has_retained_source_work(slug, meta):
            return "retained"
        try:
            has_collection = slug in _adventure_collection_names(_all_collection_names())
        except HTTPException:
            return "unavailable"
        if not has_collection and not _metadata_path(slug).is_file():
            return "missing"
        try:
            require_mutable_source(meta)
        except HTTPException:
            return "retained"
        try:
            remove_adventure_source(slug, has_collection=has_collection)
        except HTTPException:
            return "unavailable"
    return "removed"


_ACTIVE_JOB_STATES = frozenset({"queued", "running", "cancel_requested"})


async def recover_source_upload(
    slug: str,
    file: UploadFile,
    background: BackgroundTasks,
    admission: Admission | None = None,
):
    """Inert compatibility boundary: archived and prepared source bindings are immutable."""
    from ..services.campaign_sources import validate_binding

    validate_binding(slug)
    raise HTTPException(
        status_code=409,
        detail="Archived and prepared campaign sources are held and immutable. Upload a fresh adventure to use the current preparation workflow.",
    )
