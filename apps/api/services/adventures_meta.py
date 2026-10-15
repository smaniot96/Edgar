"""Adventure metadata store (JSON beside the /data PDF store).

Shared by the adventures router and the background ingest/generation workers so neither has to
import the other. Metadata persists across DB resets and tracks the ingestion lifecycle
(processing -> ready | failed) plus title/description/chunk count.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import tempfile
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypedDict

import structlog

log = structlog.get_logger()

DATA_DIR = Path("/data")
ADVENTURES_DIR = DATA_DIR / "adventures"
PDF_DIR = DATA_DIR / "pdfs"


class GenerationCheckpoint(TypedDict):
    """Durable, private generation work. Content never enters player status responses."""

    version: int
    job_id: str
    outline: dict[str, Any] | None
    chapters: dict[str, str]


class JobCancelled(Exception):
    """Raised at a safe boundary after the owner requests job cancellation."""


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
            file.flush()
            os.fsync(file.fileno())
        if create:
            # Linking a completed temporary file claims the name without overwriting it.
            # Competing API processes receive FileExistsError and choose another slug.
            os.link(temporary, meta_path(slug))
        else:
            temporary.replace(meta_path(slug))
        directory_fd = os.open(ADVENTURES_DIR, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
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


@contextmanager
def lifecycle_lock(slug: str):
    ADVENTURES_DIR.mkdir(parents=True, exist_ok=True)
    with (ADVENTURES_DIR / f"{slug}.lifecycle.lock").open("a") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def begin_attempt(
    slug: str,
    *,
    job_attempt: bool = False,
    job_kind: str = "ingestion",
    reset_job: bool = False,
) -> str:
    """Reserve a worker before dispatch; retain the complete last ready receipt."""
    with lifecycle_lock(slug):
        meta = load_meta(slug)
        if meta.get("index_receipts"):
            from .campaign_sources import active_metadata

            active = active_metadata(slug, meta)
            if active is not meta:
                # A preceding process may have activated its index and exited before
                # final JSON/canonical-file publication. Preserve that accepted receipt.
                source_attempt = active.get("source_pdf_attempt_id")
                if isinstance(source_attempt, str):
                    _publish_candidate_pdf(slug, source_attempt)
                    active.pop("source_pdf_attempt_id", None)
                meta = {**active, "index_receipts": meta["index_receipts"]}
        if meta.get("status") == "ready":
            meta["ready_receipt"] = {
                k: v
                for k, v in meta.items()
                if k not in ("attempt", "ready_receipt", "index_receipts", "last_attempt")
            }
        token = uuid.uuid4().hex
        meta.pop("last_attempt", None)
        meta.update(status="processing", error=None, failure_code=None, attempt={"id": token})
        job = meta.get("job")
        if job_attempt and (reset_job or not isinstance(job, dict)):
            job = new_job(job_kind, "queued")
            meta["job"] = job
        if job_attempt and isinstance(job, dict):
            job.update(
                kind=job_kind if reset_job else job.get("kind", job_kind),
                state="queued",
                attempt_id=token,
                retryable=False,
                updated_at=datetime.now(UTC).isoformat(),
            )
        save_meta(slug, meta)
        return token


def new_job(kind: str, stage: str, **values: Any) -> dict[str, Any]:
    """Create a stable public-safe job identity before a background task is queued."""
    now = datetime.now(UTC).isoformat()
    return {
        "version": 1,
        "job_id": uuid.uuid4().hex,
        "kind": kind,
        "state": "queued",
        "stage": stage,
        "completed_chapters": 0,
        "total_chapters": None,
        "retryable": False,
        "attempt_id": None,
        "retained_work": "none",
        "created_at": now,
        "updated_at": now,
        **values,
    }


def _restore_generation_title(meta: dict) -> None:
    """Replace the queued placeholder with a stable, non-content title on failure."""
    if (meta.get("generation") or {}).get("kind") == "ai":
        title_hint = (meta.get("generation") or {}).get("title_hint")
        meta["title"] = title_hint if isinstance(title_hint, str) and title_hint.strip() else "AI campaign"


def ensure_job(slug: str, token: str, *, kind: str, stage: str) -> dict[str, Any]:
    """Attach a job to a legacy/direct worker invocation, fenced by its attempt token."""
    with attempt_guard(slug, token) as meta:
        job = meta.get("job")
        if not isinstance(job, dict) or job.get("attempt_id") not in (None, token):
            job = new_job(kind, stage)
            meta["job"] = job
        if job.get("state") != "cancel_requested":
            job.update(state="running", stage=stage, attempt_id=token, retryable=False)
        job["updated_at"] = datetime.now(UTC).isoformat()
        save_meta(slug, meta)
        return dict(job)


def update_job(slug: str, token: str, **updates: Any) -> dict[str, Any]:
    """Atomically update scalar job status. No lock is held over provider work."""
    with attempt_guard(slug, token) as meta:
        job = meta.get("job")
        if not isinstance(job, dict) or job.get("attempt_id") != token:
            from db.vector.versions import SupersededIngestion

            raise SupersededIngestion("A newer accepted adventure attempt owns this job.")
        job.update(updates)
        job["updated_at"] = datetime.now(UTC).isoformat()
        save_meta(slug, meta)
        return dict(job)


def job_dir(slug: str, job_id: str) -> Path:
    if len(job_id) != 32 or any(c not in "0123456789abcdef" for c in job_id):
        raise ValueError("Invalid adventure job identity.")
    return ADVENTURES_DIR / slug / ".jobs" / job_id


def load_generation_checkpoint(
    slug: str, job_id: str, *, expected_chapters: int | None = None
) -> GenerationCheckpoint:
    path = job_dir(slug, job_id) / "checkpoint.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"version": 1, "job_id": job_id, "outline": None, "chapters": {}}
    if (
        not isinstance(value, dict)
        or value.get("version") != 1
        or value.get("job_id") != job_id
        or (value.get("outline") is not None and not isinstance(value.get("outline"), dict))
        or not isinstance(value.get("chapters"), dict)
        or not all(
            isinstance(key, str) and isinstance(text, str)
            for key, text in value["chapters"].items()
        )
    ):
        raise ValueError(
            "Saved generation checkpoint is unreadable; retry cannot safely resume it."
        )
    outline = value.get("outline")
    chapters = value["chapters"]
    if outline is None and chapters:
        raise ValueError("Saved chapters have no matching outline.")
    if outline is not None:
        required_outline_fields = ("title", "premise", "setting", "hook")
        if any(not isinstance(outline.get(field), str) or not outline[field].strip() for field in required_outline_fields):
            raise ValueError("Saved outline is incomplete.")
        outline_chapters = outline.get("chapters")
        if not isinstance(outline_chapters, list) or not outline_chapters:
            raise ValueError("Saved outline has no chapter list.")
        if not all(
            isinstance(chapter, dict)
            and isinstance(chapter.get("name"), str)
            and chapter["name"].strip()
            and isinstance(chapter.get("summary"), str)
            and chapter["summary"].strip()
            for chapter in outline_chapters
        ):
            raise ValueError("Saved outline contains an invalid chapter.")
        # Validate every model field (including the optional faction/NPC lists) before
        # treating this as resumable work. A structurally plausible but invalid outline
        # would otherwise fail every retry during chapter prompt construction.
        from .campaign_generator import CampaignOutline

        outline = CampaignOutline.model_validate(outline).model_dump()
        expected = len(outline_chapters)
        if expected_chapters is not None and expected != expected_chapters:
            raise ValueError("Saved outline does not match the requested campaign size.")
        if set(chapters).difference({str(index) for index in range(expected)}):
            raise ValueError("Saved chapter indexes do not match the outline.")
    elif expected_chapters is not None and chapters:
        raise ValueError("Saved chapters have no matching outline.")
    if any(not text.strip() or len(text.split()) < 20 for text in chapters.values()):
        raise ValueError("Saved chapter text is empty or incomplete.")
    return {
        "version": 1,
        "job_id": job_id,
        "outline": value.get("outline"),
        "chapters": chapters,
    }


def save_generation_checkpoint(
    slug: str,
    token: str,
    checkpoint: GenerationCheckpoint,
    *,
    stage: str,
    total_chapters: int,
) -> dict[str, Any]:
    """Write work before its metadata summary; stale attempts cannot publish checkpoints."""
    from db.vector.versions import SupersededIngestion

    with attempt_guard(slug, token) as meta:
        job = meta.get("job")
        if (
            not isinstance(job, dict)
            or job.get("attempt_id") != token
            or job.get("job_id") != checkpoint["job_id"]
            or job.get("kind") != "generation"
        ):
            raise SupersededIngestion("A newer accepted adventure attempt owns this job.")
        if job.get("state") == "cancel_requested":
            raise JobCancelled("The adventure preparation was cancelled by its owner.")
        expected_keys = {str(index) for index in range(total_chapters)}
        if set(checkpoint["chapters"]).difference(expected_keys):
            raise ValueError("Checkpoint chapter indexes do not match the requested campaign size.")
        directory = job_dir(slug, checkpoint["job_id"])
        directory.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=directory, suffix=".tmp", delete=False
            ) as file:
                temporary = Path(file.name)
                json.dump(checkpoint, file, ensure_ascii=False, separators=(",", ":"))
                file.flush()
                os.fsync(file.fileno())
            temporary.replace(directory / "checkpoint.json")
            directory_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        job.update(
            stage=stage,
            completed_chapters=len(checkpoint["chapters"]),
            total_chapters=total_chapters,
            checkpoint_available=bool(checkpoint["outline"] or checkpoint["chapters"]),
            updated_at=datetime.now(UTC).isoformat(),
        )
        save_meta(slug, meta)
        return dict(job)


@contextmanager
def attempt_guard(slug: str, token: str):
    from db.vector.versions import SupersededIngestion

    with lifecycle_lock(slug):
        meta = load_meta(slug)
        if (meta.get("attempt") or {}).get("id") != token:
            raise SupersededIngestion("A newer accepted adventure attempt owns this source.")
        yield meta


def attempt_dir(slug: str, token: str) -> Path:
    return ADVENTURES_DIR / slug / ".attempts" / token


def candidate_pdf_path(slug: str, token: str) -> Path:
    """Keep the source stem equal to the stable campaign slug throughout extraction."""
    return attempt_dir(slug, token) / f"{slug}.pdf"


def source_pdf_path(slug: str, meta: dict | None = None) -> Path:
    """Return the PDF matching the active metadata receipt, including pre-finalized receipts."""
    if meta is None:
        meta = load_meta(slug)
    token = meta.get("source_pdf_attempt_id")
    if not isinstance(token, str):
        token = meta.get("artifact_attempt")
    candidate = candidate_pdf_path(slug, token) if isinstance(token, str) else None
    if candidate is not None and candidate.is_file():
        return candidate
    return PDF_DIR / f"{slug}.pdf"


def _publish_candidate_pdf(slug: str, token: str) -> bool:
    """Atomically publish a validated recovery PDF after its index version is active."""
    candidate = candidate_pdf_path(slug, token)
    if not candidate.is_file():
        return False
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    target = PDF_DIR / f"{slug}.pdf"
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=PDF_DIR, suffix=".pdf.tmp", delete=False) as out:
            temporary = Path(out.name)
            with candidate.open("rb") as incoming:
                shutil.copyfileobj(incoming, out)
            out.flush()
            os.fsync(out.fileno())
        temporary.replace(target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    directory_fd = os.open(PDF_DIR, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return True


def set_attempt_fields(slug: str, token: str, **values: Any) -> None:
    """Attach private staged-source bookkeeping to the fenced attempt."""
    with attempt_guard(slug, token) as meta:
        meta.setdefault("attempt", {}).update(values)
        save_meta(slug, meta)


def stage_attempt_pdf(slug: str, token: str, source: Path, filename: str | None = None) -> Path:
    """Copy a candidate replacement into its immutable attempt before indexing it."""
    destination = candidate_pdf_path(slug, token)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, suffix=".tmp", delete=False) as out:
            temporary = Path(out.name)
            with source.open("rb") as incoming:
                shutil.copyfileobj(incoming, out)
            out.flush()
            os.fsync(out.fileno())
        temporary.replace(destination)
        directory_fd = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    if filename:
        set_attempt_fields(slug, token, source_filename=filename)
    update_job(
        slug,
        token,
        retained_work="source_pdf",
        retained_source_attempt_id=token,
        retained_source_filename=filename,
    )
    return destination


def cancel_attempt(slug: str, token: str) -> bool:
    """Finalize a cooperative cancellation while retaining checkpoint/source and last receipt."""
    from db.vector.versions import SupersededIngestion

    try:
        with attempt_guard(slug, token) as meta:
            previous = meta.get("ready_receipt")
            job = meta.get("job")
            if isinstance(job, dict) and job.get("attempt_id") == token:
                candidate = candidate_pdf_path(slug, token)
                job.update(
                    state="cancelled",
                    stage="cancelled",
                    retryable=not job.get("checkpoint_corrupt")
                    and (
                        job.get("kind") == "generation"
                        or candidate.is_file()
                        or (PDF_DIR / f"{slug}.pdf").is_file()
                    ),
                    updated_at=datetime.now(UTC).isoformat(),
                )
                if candidate.is_file():
                    job.update(
                        retained_work="source_pdf",
                        retained_source_attempt_id=token,
                        retained_source_filename=(meta.get("attempt") or {}).get("source_filename"),
                    )
                job.pop("attempt_id", None)
            if previous:
                meta.update(previous)
                meta["last_attempt"] = {
                    "status": "cancelled",
                    "error": "Preparation cancelled by the owner. Retained work is available to retry.",
                }
            else:
                meta.update(
                    status="failed",
                    error="Preparation cancelled by the owner. Retained work is available to retry.",
                    failure_code="cancelled",
                )
                _restore_generation_title(meta)
            if isinstance(job, dict):
                meta["job"] = job
            meta.pop("attempt", None)
            save_meta(slug, meta)
            return True
    except SupersededIngestion:
        return False


@contextmanager
def publication_guard(slug: str, token: str, *, stage: str = "publishing"):
    """Fence index activation and observe cancellation requests at its commit boundary."""
    with attempt_guard(slug, token) as meta:
        job = meta.get("job")
        if isinstance(job, dict) and job.get("attempt_id") == token:
            if job.get("state") == "cancel_requested":
                raise JobCancelled("The adventure preparation was cancelled by its owner.")
            job.update(stage=stage, updated_at=datetime.now(UTC).isoformat())
            save_meta(slug, meta)
        yield meta


def request_job_cancel(slug: str) -> dict[str, Any]:
    """Set a cooperative cancellation marker for the current worker attempt."""
    with lifecycle_lock(slug):
        meta = load_meta(slug)
        job = meta.get("job")
        attempt = meta.get("attempt") or {}
        if (
            meta.get("status") != "processing"
            or not isinstance(job, dict)
            or job.get("state") not in ("queued", "running")
            or not isinstance(attempt.get("id"), str)
            or job.get("attempt_id") != attempt.get("id")
            or job.get("stage") == "publishing"
        ):
            raise ValueError(
                "This adventure has no cancellable preparation; publication may already be committing."
            )
        job.update(
            state="cancel_requested",
            updated_at=datetime.now(UTC).isoformat(),
        )
        save_meta(slug, meta)
        return dict(job)


def check_job_running(slug: str, token: str) -> None:
    from db.vector.versions import SupersededIngestion

    meta = load_meta(slug)
    if (meta.get("attempt") or {}).get("id") != token:
        raise SupersededIngestion("A newer accepted adventure attempt owns this job.")
    job = meta.get("job")
    if isinstance(job, dict) and job.get("attempt_id") == token and job.get("state") == "cancel_requested":
        raise JobCancelled("The adventure preparation was cancelled by its owner.")


def prepare_attempt(slug: str, token: str, report: dict, updates: dict | None = None) -> None:
    """Persist the proposed receipt before activation, so a restart can reconcile it."""
    from ingestion.readiness import report_summary

    with attempt_guard(slug, token) as meta:
        summary = report_summary(report)
        meta["attempt"]["ingestion"] = summary
        if not meta.get("ready_receipt"):
            meta["ingestion"] = summary
        version = report.get("index_version")
        if version and report["quality_passed"]:
            manuscript = meta.get("manuscript")
            if isinstance(manuscript, dict) and manuscript.get("attempt_id") == token:
                manuscript.update(
                    source_sha256=report.get("source_sha256"),
                    extraction_sha256=report.get("extraction_sha256"),
                    index_version=version,
                )
            elif (
                isinstance(manuscript, dict)
                and manuscript.get("source_sha256") == report.get("source_sha256")
                and manuscript.get("extraction_sha256") == report.get("extraction_sha256")
            ):
                # Manifest-only reindexing can create a fresh vector attempt without
                # generating new prose. Keep the independent manuscript reference current.
                manuscript["index_version"] = version
            previous = meta.get("ready_receipt") or meta
            previous_ingestion = previous.get("ingestion") or {}
            source_changed = any(
                previous_ingestion.get(key) and previous_ingestion[key] != report.get(key)
                for key in ("source_sha256", "extraction_sha256")
            )
            receipt = {
                k: v
                for k, v in meta.items()
                if k not in ("attempt", "ready_receipt", "index_receipts", "last_attempt")
            }
            if source_changed:
                receipt.pop("manifest", None)
                receipt.pop("manifest_draft", None)
                meta["attempt"]["invalidate_manifest"] = True
                candidate = receipt.get("manuscript")
                if isinstance(candidate, dict) and any(
                    candidate.get(key) != report.get(key)
                    for key in ("source_sha256", "extraction_sha256")
                ):
                    receipt.pop("manuscript", None)
                    meta.pop("manuscript", None)
            candidate_pdf = candidate_pdf_path(slug, token)
            if candidate_pdf.is_file():
                receipt["source_pdf_attempt_id"] = token
                source_filename = meta["attempt"].get("source_filename")
                if isinstance(source_filename, str):
                    receipt["source_filename"] = source_filename
            receipt.update(
                updates or {},
                status="ready",
                chunks=report["chunk_count"],
                ingestion=summary,
                error=None,
                failure_code=None,
            )
            if receipt.get("manifest"):
                receipt.pop("manifest_draft", None)
            receipt["artifact_attempt"] = token
            meta.setdefault("index_receipts", {})[version] = receipt
            previous_version = (
                meta.get("ready_receipt", {}).get("ingestion") or meta.get("ingestion") or {}
            ).get("index_version")
            meta["index_receipts"] = {
                k: v for k, v in meta["index_receipts"].items() if k in (version, previous_version)
            }
        save_meta(slug, meta)


def finish_attempt(slug: str, token: str, updates: dict, *, success: bool = False) -> bool:
    """Only the current worker may publish artifacts/metadata or record a failure."""
    from db.vector.versions import SupersededIngestion

    try:
        with attempt_guard(slug, token) as meta:
            previous = meta.get("ready_receipt")
            report = meta["attempt"].get("ingestion")
            job = meta.get("job")
            if isinstance(job, dict) and job.get("attempt_id") == token:
                candidate_pdf = candidate_pdf_path(slug, token)
                job.update(
                    state="completed" if success else "failed",
                    stage="completed" if success else job.get("stage", "failed"),
                    retryable=not success
                    and not job.get("checkpoint_corrupt")
                    and (
                        job.get("kind") == "generation"
                        or candidate_pdf.is_file()
                        or (PDF_DIR / f"{slug}.pdf").is_file()
                    ),
                    updated_at=datetime.now(UTC).isoformat(),
                )
                if success:
                    job.update(retained_work="published", retained_source_attempt_id=None)
                elif candidate_pdf.is_file():
                    job.update(
                        retained_work="source_pdf",
                        retained_source_attempt_id=token,
                        retained_source_filename=(meta.get("attempt") or {}).get(
                            "source_filename"
                        ),
                    )
            if success or not previous:
                staged = attempt_dir(slug, token)
                destination = ADVENTURES_DIR / slug
                destination.mkdir(parents=True, exist_ok=True)
                for path in staged.glob(f"{slug}.*"):
                    if path.suffix.lower() == ".pdf":
                        continue
                    # Retain the immutable attempt copy; the canonical manuscript paths
                    # remain backward compatible for local review and retry.
                    with tempfile.NamedTemporaryFile(dir=destination, delete=False) as out:
                        temporary = Path(out.name)
                    try:
                        shutil.copyfile(path, temporary)
                        temporary.replace(destination / path.name)
                    finally:
                        temporary.unlink(missing_ok=True)
                if success and meta.get("attempt", {}).get("invalidate_manifest"):
                    meta.pop("manifest", None)
                    meta.pop("manifest_draft", None)
                meta.update(updates)
                if report:
                    meta["ingestion"] = report
                if success:
                    meta["artifact_attempt"] = token
                    attempt = meta.get("attempt") or {}
                    if _publish_candidate_pdf(slug, token):
                        if isinstance(attempt.get("source_filename"), str):
                            meta["source_filename"] = attempt["source_filename"]
                    meta.pop("source_pdf_attempt_id", None)
                    meta.pop("ready_receipt", None)
                    if updates.get("manifest"):
                        meta.pop("manifest_draft", None)
                elif not previous:
                    _restore_generation_title(meta)
            else:
                job = meta.get("job")
                meta.update(previous)
                if isinstance(job, dict):
                    meta["job"] = job
                meta["last_attempt"] = updates
                _restore_generation_title(meta)
            meta.pop("attempt", None)
            save_meta(slug, meta)
            return True
    except SupersededIngestion:
        return False


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
        with lifecycle_lock(slug):
            meta = load_meta(slug)
            if meta.get("status") != "processing":
                continue
            current_attempt = (meta.get("attempt") or {}).get("id")
            current_candidate = (
                candidate_pdf_path(slug, current_attempt)
                if isinstance(current_attempt, str)
                else None
            )
            accepted = None
            if meta.get("index_receipts"):
                from .campaign_sources import active_metadata

                active = active_metadata(slug, meta)
                if current_attempt and active.get("artifact_attempt") == current_attempt:
                    accepted = active
                    _publish_candidate_pdf(slug, current_attempt)
                    accepted.pop("source_pdf_attempt_id", None)
            job = meta.get("job")
            if isinstance(job, dict) and job.get("state") in ("queued", "running"):
                job.update(
                    state="interrupted",
                    retryable=bool(
                        not job.get("checkpoint_corrupt")
                        and (
                            job.get("kind") == "generation"
                            or (current_candidate is not None and current_candidate.is_file())
                            or (PDF_DIR / f"{slug}.pdf").is_file()
                        )
                    ),
                    interruption="server_restart",
                    updated_at=datetime.now(UTC).isoformat(),
                )
                if current_candidate is not None and current_candidate.is_file():
                    job.update(
                        retained_work="source_pdf",
                        retained_source_attempt_id=current_attempt,
                        retained_source_filename=(meta.get("attempt") or {}).get("source_filename"),
                    )
                job.pop("attempt_id", None)
            elif isinstance(job, dict) and job.get("state") == "cancel_requested":
                job.update(
                    state="cancelled",
                    stage="cancelled",
                    retryable=bool(
                        not job.get("checkpoint_corrupt")
                        and (
                            job.get("kind") == "generation"
                            or (current_candidate is not None and current_candidate.is_file())
                            or (PDF_DIR / f"{slug}.pdf").is_file()
                        )
                    ),
                    updated_at=datetime.now(UTC).isoformat(),
                )
            previous = meta.get("ready_receipt")
            if accepted is not None and accepted.get("status") == "ready":
                meta.update(accepted)
                if isinstance(job, dict):
                    job.update(state="completed", stage="completed", retryable=False)
                    meta["job"] = job
                job = None
            elif previous:
                meta.update(previous)
                if isinstance(job, dict):
                    meta["job"] = job
                meta["last_attempt"] = {"status": "failed", "error": STALE_PROCESSING_ERROR}
            else:
                meta.update({"status": "failed", "error": STALE_PROCESSING_ERROR})
                _restore_generation_title(meta)
            meta.pop("attempt", None)
            save_meta(slug, meta)
            stale.append(slug)
    return stale
