"""Source availability and the explicit module/homebrew play contract (no DB writes)."""

from __future__ import annotations

import re

from fastapi import HTTPException
from starlette.concurrency import run_in_threadpool

from db.vector.collections import is_campaign_lore_collection, is_rules_collection
from db.vector.source_status import inspect_source
from db.vector.versions import active_index_version

from ..schemas.adventure import AdventureJobRead
from ..schemas.campaign import CampaignRead, SourceRead
from .adventures_meta import PDF_DIR as PDF_DIR, load_meta, source_pdf_path


def validate_binding(slug: str) -> None:
    if (
        not re.fullmatch(r"[a-z0-9][a-z0-9_\-]{0,254}", slug)
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
        "queued", "running", "interrupted", "failed", "completed", "cancel_requested", "cancelled"
    }
    allowed_stages = {
        "queued", "outline", "generating_chapters", "extracting", "embedding", "indexing",
        "publishing", "completed", "failed", "cancelled",
    }
    if not isinstance(job_id, str) or not re.fullmatch(r"[0-9a-f]{32}", job_id):
        return None
    if meta.get("private_preparation") is True:
        from .preparation_runtime import safe_preparation_status

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
        action_copy = {
            "wait": "Preparation is in progress. Check back for an update.",
            "retry": "Saved preparation work can be retried.",
            "restart": "Preparation needs attention before it can continue.",
            "none": "Your adventure is ready to play.",
        }.get(projected.action)
        return AdventureJobRead(
            version=1,
            job_id=job_id,
            kind="generation" if (meta.get("generation") or {}).get("kind") == "ai" else "ingestion",
            state=public_state,
            stage=projected.stage,
            completed_chapters=0,
            total_chapters=None,
            checkpoint_available=bool(meta.get("preparation_run_id")),
            retryable=projected.retryable,
            retained_work=(job.get("retained_work") if job.get("retained_work") in {
                "none", "outline_and_chapters", "complete_manuscript", "source_pdf", "inspected_extraction", "published"
            } else "none"),
            action_label=action_copy,
            explanation=action_copy,
            source_version=(meta.get("prepared_bundle") or {}).get("bundle_sha256")
            if projected.status.value == "ready" else None,
        )
    if kind not in ("generation", "ingestion") or state not in allowed_states:
        return None
    total = job.get("total_chapters")
    total = total if isinstance(total, int) and 0 < total <= 24 else None
    chapters = job.get("completed_chapters", 0)
    chapters = chapters if isinstance(chapters, int) and 0 <= chapters <= 24 else 0
    checkpoint = bool(job.get("checkpoint_available"))
    retryable = bool(job.get("retryable")) and not bool(job.get("checkpoint_corrupt"))
    retained_work = "none"
    action_label = explanation = None
    if kind == "generation":
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
            retryable = False
            explanation = (
                "Saved generation progress is unreadable. Create a new campaign to generate again; "
                "any previously published source remains available."
            )
        elif complete:
            retained_work = "complete_manuscript"
            action_label = "Rebuild index from saved manuscript"
            explanation = "The outline and every chapter are saved. Retrying rebuilds the index without another generation call."
        elif checkpoint:
            retained_work = "outline_and_chapters"
            action_label = "Resume saved chapters"
            explanation = (
                f"The outline and {chapters} of {total or 'the required'} chapters are saved. "
                "Retry generates only chapters that are still missing."
            )
        else:
            action_label = "Restart generation"
            explanation = "No generation checkpoint is available. Retrying starts over and repeats paid model generation calls."
    elif job.get("retained_source_attempt_id") or source_pdf_path(slug, meta).is_file():
        retained_work = "source_pdf"
        action_label = "Retry extraction and indexing"
        explanation = "A retained PDF is available; retry reruns extraction and indexing without model generation."
    ingestion = meta.get("ingestion") or {}
    source_version = ingestion.get("index_version") if isinstance(ingestion, dict) else None
    if not isinstance(source_version, str):
        source_version = None
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
        from .preparation_runtime import private_preparation_ready

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
    if raw_meta.get("private_preparation") is True:
        from .preparation_runtime import private_preparation_ready, safe_preparation_status

        if not private_preparation_ready(slug, raw_meta):
            projected = safe_preparation_status(raw_meta, slug)
            failed = projected is not None and projected.status.value in ("blocked", "failed", "cancelled")
            return SourceRead(
                slug=slug,
                title="Private adventure",
                status="failed" if failed else "processing",
                usable_chunks=0,
                expected_chunks=None,
                collection_exists=False,
                source_pdf_available=False,
                message=(
                    "Private preparation needs attention. Retry the saved work or start a new adventure."
                    if failed else "Your adventure is being prepared privately."
                ),
                failure_code="preparation_blocked" if failed else None,
                extraction_preview_available=False,
                extraction_preview_current=False,
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
    from .adventures_meta import ADVENTURES_DIR, attempt_dir, candidate_pdf_path

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
        else attempt_dir(slug, token) if token else ADVENTURES_DIR / slug
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
    return SourceRead(
        **live,
        title=meta.get("title") or slug.replace("_", " ").title(),
        source_pdf_available=(
            False if is_private_preparation else source_pdf_path(slug, meta).is_file()
        ),
        message=meta.get("error")
        if live["status"] == "failed" and meta.get("error")
        else messages.get(live["status"]),
        failure_code=meta.get("failure_code"),
        extraction_preview_available=preview_available,
        extraction_preview_current=preview_current,
        ingestion=ingestion if isinstance(ingestion, dict) else None,
        job=safe_job_read(raw_meta, slug),
    )


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
    return result
