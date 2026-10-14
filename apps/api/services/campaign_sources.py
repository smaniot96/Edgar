"""Source availability and the explicit module/homebrew play contract (no DB writes)."""

from __future__ import annotations

import re

from fastapi import HTTPException
from starlette.concurrency import run_in_threadpool

from db.vector.collections import is_campaign_lore_collection, is_rules_collection
from db.vector.source_status import inspect_source

from ..schemas.campaign import CampaignRead, SourceRead
from .adventures_meta import PDF_DIR, load_meta


def validate_binding(slug: str) -> None:
    if (
        not re.fullmatch(r"[a-z0-9][a-z0-9_\-]{0,254}", slug)
        or is_rules_collection(slug)
        or is_campaign_lore_collection(slug)
    ):
        raise HTTPException(status_code=422, detail="Invalid adventure source binding.")


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
        pdf = PDF_DIR / f"{slug}.pdf"
        try:
            from ingestion.artifacts import source_digest

            source_file_matches = source_digest(pdf) == ingestion.get("source_sha256")
        except OSError:
            source_file_matches = False
        if not source_file_matches and live["status"] == "ready":
            live["status"] = "partial"
    live["source_file_matches"] = source_file_matches
    return live


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
    meta = load_meta(slug)
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
    from .adventures_meta import ADVENTURES_DIR

    preview_available = (ADVENTURES_DIR / slug / f"{slug}.extraction.json").is_file() and (
        ADVENTURES_DIR / slug / f"{slug}.md"
    ).is_file()
    return SourceRead(
        **live,
        title=meta.get("title") or slug.replace("_", " ").title(),
        source_pdf_available=(PDF_DIR / f"{slug}.pdf").is_file(),
        message=meta.get("error")
        if live["status"] == "failed" and meta.get("error")
        else messages.get(live["status"]),
        failure_code=meta.get("failure_code"),
        extraction_preview_available=preview_available,
        extraction_preview_current=preview_available and live["source_file_matches"],
        ingestion=ingestion if isinstance(ingestion, dict) else None,
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
