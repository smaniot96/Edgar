"""Source availability and the explicit module/homebrew play contract (no DB writes)."""

from __future__ import annotations

import hashlib
import re

from fastapi import HTTPException
from starlette.concurrency import run_in_threadpool

from db.vector.collections import is_campaign_lore_collection, is_rules_collection
from db.vector.source_status import inspect_source
from db.vector.versions import active_index_version
from edgar_core.preparation_status import SafePreparationFailure
from edgar_core.safe_errors import OutcomeState, classify_failure

from ..schemas.adventure import AdventureJobRead
from ..schemas.campaign import CampaignRead, SourceRead
from .adventures_meta import (
    PDF_DIR as PDF_DIR,
    adventure_path,
    load_meta,
    source_pdf_path,
    valid_adventure_slug,
)


def validate_binding(slug: str) -> None:
    if (
        not valid_adventure_slug(slug)
        or is_rules_collection(slug)
        or is_campaign_lore_collection(slug)
    ):
        raise HTTPException(status_code=422, detail="Invalid adventure source binding.")


def safe_job_read(meta: dict, slug: str) -> AdventureJobRead | None:
    """Project only stable scalar lifecycle facts; checkpoint content remains private."""
    job = meta.get("job")
    if not isinstance(job, dict):
        return None
    kind, state, job_id = job.get("kind"), job.get("state"), job.get("job_id")
    allowed_states = {
        "queued",
        "running",
        "interrupted",
        "failed",
        "completed",
        "cancel_requested",
        "cancelled",
    }
    allowed_stages = {
        "queued",
        "outline",
        "generating_chapters",
        "extracting",
        "embedding",
        "indexing",
        "publishing",
        "completed",
        "failed",
        "cancelled",
    }
    if not isinstance(job_id, str) or not re.fullmatch(r"[0-9a-f]{32}", job_id):
        return None
    if meta.get("private_preparation") is True:
        from .campaign_prep.runtime import safe_preparation_status

        projected = safe_preparation_status(meta, slug)
        if projected is None:
            return None
        public_state = {
            "queued": "queued",
            "preparing": "running",
            "reviewing": "running",
            "repairing": "running",
            "publishing": "running",
            "ready": "completed",
            "blocked": "failed",
            "failed": "failed",
            "cancelled": "cancelled",
        }.get(projected.status.value, "running")
        saved_run = bool(meta.get("preparation_run_id") or meta.get("preparation_workflow"))
        # A short button label and a separate sentence, so the card never repeats itself.
        action_label = "Retry preparation" if projected.action == "retry" else None
        explanation = {
            "wait": "Preparation is in progress. Check back for an update.",
            "retry": "Saved preparation work is kept. Retrying continues from the last accepted step."
            if saved_run
            else "No preparation work was saved before this failure. Retrying starts again from the same source and setup.",
            "restart": "Archived preparation is held. Its saved work is preserved. Create a fresh adventure to use the new workflow.",
            "none": "Your adventure is ready to play."
            if projected.status.value == "ready"
            else "Preparation was cancelled. Saved work is retained."
            if projected.status.value == "cancelled"
            else "Preparation is held. Its publication needs verification before play.",
        }.get(projected.action)
        return AdventureJobRead(
            version=1,
            job_id=job_id,
            kind="generation"
            if (meta.get("generation") or {}).get("kind") == "ai"
            else "ingestion",
            state=public_state,
            stage=projected.stage,
            completed_chapters=0,
            total_chapters=None,
            checkpoint_available=saved_run,
            retryable=projected.retryable,
            retained_work=(
                projected.retained_work
                if projected.retained_work != "none"
                else job.get("retained_work")
                if job.get("retained_work")
                in {
                    "none",
                    "outline_and_chapters",
                    "complete_manuscript",
                    "source_pdf",
                    "inspected_extraction",
                    "published",
                }
                else "none"
            ),
            action_label=action_label,
            explanation=projected.failure.message if projected.failure else explanation,
            source_version=(meta.get("prepared_bundle") or {}).get("bundle_sha256")
            if projected.status.value == "ready"
            else None,
            failure=projected.failure,
        )
    if kind not in ("generation", "ingestion") or state not in allowed_states:
        return None
    total = job.get("total_chapters")
    total = total if isinstance(total, int) and 0 < total <= 24 else None
    chapters = job.get("completed_chapters", 0)
    chapters = chapters if isinstance(chapters, int) and 0 <= chapters <= 24 else 0
    checkpoint = bool(job.get("checkpoint_available"))
    # Every non-private job reaching this branch belongs to the retired source workers.
    # Its retained receipts remain readable, but no active retry can use those executors.
    retryable = False
    retained_work = "none"
    action_label = explanation = None
    if kind == "generation":
        # Archived chapter jobs remain readable, but their retired worker cannot resume.
        retryable = False
        checkpoint_error = bool(job.get("checkpoint_corrupt"))
        try:
            from .adventures_meta import load_generation_checkpoint

            saved = load_generation_checkpoint(slug, job_id, expected_chapters=total)
            checkpoint = bool(saved["outline"] or saved["chapters"])
            saved_chapters = saved["chapters"]
            chapters = len(saved_chapters)
            if any(not key.isdigit() or int(key) >= (total or 0) for key in saved_chapters):
                checkpoint_error = True
            complete = bool(
                saved["outline"]
                and total is not None
                and chapters == total
                and set(saved_chapters) == {str(index) for index in range(total)}
            )
        except (OSError, ValueError):
            complete = False
            checkpoint_error = True
        if checkpoint_error:
            checkpoint = False
            explanation = (
                "Saved generation progress is unreadable. Its files and any previously "
                "published source are preserved."
            )
        elif complete:
            retained_work = "complete_manuscript"
            explanation = "The outline and every chapter are saved."
        elif checkpoint:
            retained_work = "outline_and_chapters"
            explanation = (
                f"The outline and {chapters} of {total or 'the required'} chapters are saved."
            )
        else:
            explanation = "No generation checkpoint is available."
        if state != "completed":
            explanation += " Archived preparation is held. Create a fresh adventure to use the current workflow."
    elif job.get("retained_source_attempt_id") or source_pdf_path(slug, meta).is_file():
        retained_work = "source_pdf"
        explanation = "The retained PDF is preserved. Archived preparation is held. Create a fresh adventure to use the current workflow."
    ingestion = meta.get("ingestion") or {}
    source_version = ingestion.get("index_version") if isinstance(ingestion, dict) else None
    if not isinstance(source_version, str):
        source_version = None
    safe_failure = _job_failure(meta, job, state, job_id)
    if safe_failure is not None:
        safe_failure = safe_failure.model_copy(
            update={"retryable": False, "recovery_action": "read_history"}
        )
    return AdventureJobRead(
        version=1,
        job_id=job_id,
        kind=kind,
        state=state,
        stage=job.get("stage") if job.get("stage") in allowed_stages else state,
        completed_chapters=chapters,
        total_chapters=total,
        checkpoint_available=checkpoint,
        retryable=retryable,
        retained_work=retained_work,
        action_label=action_label,
        explanation=explanation,
        source_version=source_version,
        failure=safe_failure,
    )


def _job_failure(meta: dict, job: dict, state: str, job_id: str) -> SafePreparationFailure | None:
    """Project a persisted job failure through fixed copy and a stable reference."""
    if state not in ("failed", "interrupted", "cancelled"):
        return None
    if state == "interrupted":
        code = "turn_interrupted"
    elif state == "cancelled":
        code = "cancelled"
    else:
        last_attempt = meta.get("last_attempt")
        last_attempt = last_attempt if isinstance(last_attempt, dict) else {}
        code = meta.get("failure_code") or last_attempt.get("failure_code") or "turn_failed"
    last_attempt = meta.get("last_attempt")
    last_attempt = last_attempt if isinstance(last_attempt, dict) else {}
    ref = meta.get("support_ref") or job.get("support_ref") or last_attempt.get("support_ref")
    if not isinstance(ref, str) or not re.fullmatch(r"err-[0-9a-f]{16}", ref):
        ref = "err-" + hashlib.sha256(f"{job_id}:{code}".encode()).hexdigest()[:16]
    safe = classify_failure(code, outcome=OutcomeState.UNKNOWN, support_ref=ref)
    return SafePreparationFailure(
        code=safe.code,
        category=safe.category.value,
        message=safe.message,
        recovery_action=safe.recovery_action.value,
        retryable=safe.retryable,
        support_ref=safe.support_ref,
        outcome=safe.outcome.value,
    )


def _stored_failure(meta: dict, identity: str) -> SafePreparationFailure | None:
    """Safely expose source failure metadata when a legacy record has no job object."""
    last_attempt = meta.get("last_attempt")
    last_attempt = last_attempt if isinstance(last_attempt, dict) else {}
    code = meta.get("failure_code") or last_attempt.get("failure_code")
    if not isinstance(code, str):
        return None
    ref = meta.get("support_ref") or last_attempt.get("support_ref")
    if not isinstance(ref, str) or not re.fullmatch(r"err-[0-9a-f]{16}", ref):
        ref = "err-" + hashlib.sha256(f"{identity}:{code}".encode()).hexdigest()[:16]
    safe = classify_failure(code, outcome=OutcomeState.UNKNOWN, support_ref=ref)
    return SafePreparationFailure(
        code=safe.code,
        category=safe.category.value,
        message=safe.message,
        recovery_action=safe.recovery_action.value,
        retryable=safe.retryable,
        support_ref=safe.support_ref,
        outcome=safe.outcome.value,
    )


def inspect_ingestion_source(slug: str, meta: dict) -> dict:
    """The single live quality/version/coverage assessment used by workers and read APIs."""
    expected = meta.get("chunks")
    expected = expected if isinstance(expected, int) and expected > 0 else None
    ingestion = meta.get("ingestion")
    checks = {}
    if isinstance(ingestion, dict):
        checks = {
            "expected_source_hash": ingestion.get("source_sha256"),
            "expected_extraction_hash": ingestion.get("extraction_sha256"),
            "expected_pages": ingestion.get("required_pages"),
        }
    live = inspect_source(slug, expected_chunks=expected, **checks)
    source_file_matches = True
    if isinstance(ingestion, dict) and live["status"] == "ready":
        if not ingestion.get("quality_passed"):
            live["status"] = "failed"
    if isinstance(ingestion, dict) and ingestion.get("source_kind") == "pdf":
        # The retained file must still be the validated source, not a replacement under
        # the same slug. Legacy sources without a report keep EDG-001's live checks.
        pdf = source_pdf_path(slug, meta)
        try:
            from ingestion.artifacts import source_digest

            source_file_matches = source_digest(pdf) == ingestion.get("source_sha256")
        except OSError:
            source_file_matches = False
        if not source_file_matches and live["status"] == "ready":
            live["status"] = "partial"
    live["source_file_matches"] = source_file_matches
    return live


def active_metadata(slug: str, meta: dict | None = None) -> dict:
    """Select the durable receipt matching Qdrant's atomic activation after a restart.

    Receipts are written before promotion. Until promotion the previous ready receipt
    remains authoritative; afterward a crash before the final JSON update is recoverable.
    """
    meta = load_meta(slug) if meta is None else meta
    if meta.get("private_preparation") is True:
        from .campaign_prep.runtime import private_preparation_ready

        if not private_preparation_ready(slug, meta):
            # A staged candidate can already have an activated vector version. Keep every
            # legacy metadata/index consumer blind until checkpoint and metadata receipts agree.
            return {"private_preparation": True, "status": "processing"}
        return meta
    receipts = meta.get("index_receipts") or {}
    if receipts:
        try:
            version = active_index_version(slug)
        except Exception:
            version = None
        if (
            meta.get("status") == "ready"
            and (meta.get("ingestion") or {}).get("index_version") == version
        ):
            return meta
        receipt = receipts.get(version)
        if receipt is not None:
            return receipt
    if meta.get("ready_receipt") and meta.get("status") in ("processing", "failed"):
        return meta["ready_receipt"]
    return meta


def source_read(slug: str) -> SourceRead:
    try:
        validate_binding(slug)
    except HTTPException:
        return SourceRead(
            slug=slug,
            title=slug,
            status="unavailable",
            message="This legacy source binding is invalid. Bind a valid adventure source or choose homebrew play.",
        )
    raw_meta = load_meta(slug)
    if raw_meta.get("preparation_workflow") == "campaign-graph-2.0":
        from .campaign_prep.runtime import private_preparation_ready
        from .campaign_prep.store import Store

        try:
            state = Store(slug).state().get("status", "queued")
            ready = private_preparation_ready(slug, raw_meta)
            status = (
                "ready"
                if ready
                else "failed"
                if state in ("ready", "blocked", "cancelled")
                else "processing"
            )
        except Exception:
            # Database/vector outages are an unavailable read, never evidence of Ready.
            # Keep provider receipts, private diagnostics and filesystem paths out of DTOs.
            ready, state, status = False, "unavailable", "unavailable"
        message = {
            "ready": None,
            "processing": "Your adventure is being prepared privately.",
            "unavailable": "The prepared adventure cannot be checked right now. Try again when the store is available.",
        }.get(status, "Preparation is saved and needs attention.")
        if state == "cancelled":
            message = "Preparation was cancelled. Its saved work is retained."
        try:
            public_job = safe_job_read(raw_meta, slug)
        except Exception:
            public_job = None
        if public_job is not None and state == "ready" and not ready:
            public_job = public_job.model_copy(
                update={
                    "state": "failed",
                    "stage": "Published records need attention",
                    "retryable": False,
                    "action_label": None,
                    "explanation": message,
                }
            )
        return SourceRead(
            slug=slug,
            title=raw_meta.get("title") or "Private adventure",
            status=status,
            usable_chunks=(raw_meta.get("chunks") or 0) if ready else 0,
            expected_chunks=raw_meta.get("chunks"),
            collection_exists=ready,
            source_pdf_available=False,
            message=message,
            job=public_job,
            ingestion=raw_meta.get("ingestion") if ready else None,
        )
    if raw_meta.get("private_preparation") is True:
        from .campaign_prep.runtime import private_preparation_ready, safe_preparation_status

        if not private_preparation_ready(slug, raw_meta):
            from .campaign_prep.runtime import failed_source_preview_dir

            projected = safe_preparation_status(raw_meta, slug)
            failed = projected is not None and projected.status.value in (
                "blocked",
                "failed",
                "cancelled",
            )
            # Only a PDF that failed before any run existed has an inspectable extraction.
            failed_preview = failed and failed_source_preview_dir(slug, raw_meta) is not None
            return SourceRead(
                slug=slug,
                title="Private adventure",
                status="failed" if failed else "processing",
                usable_chunks=0,
                expected_chunks=None,
                collection_exists=False,
                source_pdf_available=False,
                message=(
                    projected.failure.message
                    if failed and projected and projected.failure
                    else "Private preparation needs attention. Check its saved status."
                    if failed
                    else "Your adventure is being prepared privately."
                ),
                failure_code=projected.failure.code
                if failed and projected and projected.failure
                else None,
                extraction_preview_available=failed_preview,
                extraction_preview_current=failed_preview,
                ingestion=None,
                job=safe_job_read(raw_meta, slug),
            )
    meta = active_metadata(slug, raw_meta)
    ingestion = meta.get("ingestion")
    live = inspect_ingestion_source(slug, meta)
    if meta.get("status") in ("processing", "failed") and live["status"] != "unavailable":
        live["status"] = meta["status"]
    messages = {
        "missing": "The bound adventure collection is missing. Provide its source PDF to recover it.",
        "empty": "The adventure index has no usable text and vectors. Provide its source PDF again.",
        "partial": "The adventure index or retained PDF does not match its validated source coverage. Inspect the extraction preview and recover the source.",
        "unavailable": "The adventure index cannot be checked right now. Try again when the store is available.",
        "processing": "The adventure source is being prepared.",
        "failed": "The adventure source could not be prepared. Provide its source PDF again.",
    }
    from .adventures_meta import attempt_dir, candidate_pdf_path

    job = raw_meta.get("job") or {}
    candidate_id = job.get("retained_source_attempt_id")
    if candidate_id is None and job.get("kind") == "ingestion":
        candidate_id = job.get("attempt_id")
    candidate_preview = attempt_dir(slug, candidate_id) if isinstance(candidate_id, str) else None
    token = meta.get("artifact_attempt")
    preview_dir = (
        candidate_preview
        if candidate_preview is not None
        and (candidate_preview / f"{slug}.extraction.json").is_file()
        and (candidate_preview / f"{slug}.md").is_file()
        else attempt_dir(slug, token)
        if token
        else adventure_path(slug)
    )
    preview_path = preview_dir / f"{slug}.extraction.json"
    is_private_preparation = raw_meta.get("private_preparation") is True
    preview_available = (
        not is_private_preparation
        and preview_path.is_file()
        and (preview_dir / f"{slug}.md").is_file()
    )
    preview_current = preview_available and live["source_file_matches"]
    if preview_available and candidate_preview == preview_dir:
        try:
            import json

            preview_data = json.loads(preview_path.read_text(encoding="utf-8"))
            readiness = preview_data.get("readiness") if isinstance(preview_data, dict) else None
            if not isinstance(readiness, dict):
                preview_current = False
            elif readiness.get("source_kind", "pdf") == "generated":
                expected_hash = (meta.get("ingestion") or {}).get("source_sha256")
                preview_current = expected_hash == preview_data.get("source_sha256")
            else:
                from ingestion.artifacts import source_digest

                candidate_pdf = candidate_pdf_path(slug, candidate_id)
                if not candidate_pdf.is_file():
                    candidate_pdf = source_pdf_path(slug, meta)
                preview_current = source_digest(candidate_pdf) == preview_data.get("source_sha256")
        except (OSError, ValueError):
            preview_current = False
    public_job = safe_job_read(raw_meta, slug)
    failure = public_job.failure if public_job else _stored_failure(raw_meta, slug)
    return SourceRead(
        **live,
        title=meta.get("title") or slug.replace("_", " ").title(),
        source_pdf_available=(
            False if is_private_preparation else source_pdf_path(slug, meta).is_file()
        ),
        message=(failure.message if failure else messages.get(live["status"])),
        failure_code=failure.code if failure else None,
        extraction_preview_available=preview_available,
        extraction_preview_current=preview_current,
        ingestion=ingestion if isinstance(ingestion, dict) else None,
        job=public_job,
    )


def is_private_preparation_source(slug: str) -> bool:
    """Inspect the internal source kind without inferring it from player-facing copy."""
    return load_meta(slug).get("private_preparation") is True


async def source_reads(slugs: list[str]) -> list[SourceRead]:
    return await run_in_threadpool(lambda: [source_read(s) for s in dict.fromkeys(slugs)])


def source_error(sources: list[SourceRead]) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "code": "campaign_sources_unavailable",
            "message": "Recover the campaign's adventure sources or explicitly choose homebrew play.",
            "retrieval_degraded": True,
            "sources": [s.model_dump() for s in sources],
        },
    )


async def require_sources(slugs: list[str], mode: str) -> list[SourceRead]:
    sources = await source_reads(slugs)
    if mode != "homebrew" and (not sources or any(s.status != "ready" for s in sources)):
        raise source_error(sources)
    return sources


async def require_campaign_sources(campaign) -> list[SourceRead]:
    return await require_sources(list(campaign.adventure_collections or []), campaign.source_mode)


async def campaign_read(campaign) -> CampaignRead:
    result = CampaignRead.model_validate(campaign)
    result.sources = await source_reads(result.adventure_collections)
    result.retrieval_degraded = any(s.status != "ready" for s in result.sources) or (
        result.source_mode == "module" and not result.sources
    )
    result.play_allowed = result.source_mode == "homebrew" or not result.retrieval_degraded
    # `play_allowed` keeps its historical index-only meaning for existing API clients.
    # New callers use the additive full projection, which includes review/version gates.
    from .campaign_readiness import evaluate_play_readiness

    result.readiness = await evaluate_play_readiness(
        None, campaign, observed_sources=result.sources
    )
    return result
