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
    expected = meta.get("chunks")
    expected = expected if isinstance(expected, int) and expected > 0 else None
    live = inspect_source(slug, expected_chunks=expected)
    if meta.get("status") in ("processing", "failed") and live["status"] != "unavailable":
        live["status"] = meta["status"]
    messages = {
        "missing": "The bound adventure collection is missing. Provide its source PDF to recover it.",
        "empty": "The adventure index has no usable text and vectors. Provide its source PDF again.",
        "partial": "The adventure index has fewer usable chunks than its recorded ingestion.",
        "unavailable": "The adventure index cannot be checked right now. Try again when the store is available.",
        "processing": "The adventure source is being prepared.",
        "failed": "The adventure source could not be prepared. Provide its source PDF again.",
    }
    return SourceRead(
        **live,
        title=meta.get("title") or slug.replace("_", " ").title(),
        source_pdf_available=(PDF_DIR / f"{slug}.pdf").is_file(),
        message=messages.get(live["status"]),
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
