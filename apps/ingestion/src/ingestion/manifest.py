"""Compile, inspect and explicitly review exact extraction spans before indexing."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from edgar_core.manifests import (
    AdventureManifest,
    ManifestError,
    manifest_digest,
    require_published,
)
from ingestion.artifacts import text_digest

if TYPE_CHECKING:
    from ingestion.extract import Extraction


def compile_manifest(
    extraction: Extraction, *, rules_edition: str = "unspecified",
    degraded_page_budget: float | None = None,
) -> AdventureManifest:
    """Deterministic page/paragraph candidates, deliberately without audience or opening claims.

    Reviewers edit this JSON: merge/rename scene IDs, add actors/clues/hazards/endings,
    split exact spans if a paragraph mixes public and private facts, and classify all text.
    """
    extraction.require_valid(degraded_page_budget)
    scenes, passages = [], []
    for page, text in extraction.text_pages():
        if not text.strip():
            continue
        scene_id = f"scene_p{page:04d}"
        scenes.append({"scene_id": scene_id, "title": f"Page {page} — review scene boundary"})
        for match in re.finditer(r"\S(?:.*?\S)?(?=\n\s*\n|\Z)", text, re.DOTALL):
            passages.append(
                {
                    "passage_id": f"passage_p{page:04d}_{match.start():06d}",
                    "page": page,
                    "start": match.start(),
                    "end": match.end(),
                    "text_sha256": text_digest(match.group()),
                    "scene_ids": [scene_id],
                    "audience": "unreviewed",
                }
            )
    return AdventureManifest(
        source=extraction.source,
        source_sha256=extraction.source_sha256,
        extraction_sha256=extraction.text_sha256(),
        rules_edition=rules_edition,
        scenes=scenes,
        passages=passages,
    )


def validate_spans(
    extraction: Extraction, manifest: AdventureManifest, *, check_digests: bool = True,
    degraded_page_budget: float | None = None,
) -> None:
    """Every source character is classified exactly once; whitespace may remain between spans."""
    extraction.require_valid(degraded_page_budget)
    if (
        manifest.source != extraction.source
        or manifest.source_sha256 != extraction.source_sha256
        or manifest.extraction_sha256 != extraction.text_sha256()
    ):
        raise ManifestError("Manifest does not match this exact source/extraction bundle.")
    if (
        extraction.rules_edition != "unspecified"
        and manifest.rules_edition != extraction.rules_edition
    ):
        raise ManifestError("Manifest edition conflicts with its reviewed extraction declaration.")
    pages = dict(extraction.text_pages())
    if any(p.page not in pages for p in manifest.passages):
        raise ManifestError("Manifest passage refers to a missing physical page.")
    for number, text in pages.items():
        cursor = 0
        spans = sorted((p for p in manifest.passages if p.page == number), key=lambda p: p.start)
        for passage in spans:
            if passage.start < cursor or passage.end > len(text):
                raise ManifestError("Manifest spans overlap or extend beyond their source page.")
            if text[cursor : passage.start].strip():
                raise ManifestError("Manifest leaves unclassified source text before a passage.")
            if not text[passage.start : passage.end].strip():
                raise ManifestError("Manifest passage contains only whitespace.")
            if check_digests and passage.text_sha256 != text_digest(
                text[passage.start : passage.end]
            ):
                raise ManifestError("Passage checksum does not match its exact source span.")
            cursor = passage.end
        if text[cursor:].strip():
            raise ManifestError("Manifest leaves unclassified source text at the end of a page.")


def review_manifest(
    extraction: Extraction,
    value: dict | AdventureManifest,
    *,
    reviewed_digest: str,
    reviewer: str,
    degraded_page_budget: float | None = None,
) -> AdventureManifest:
    """Acknowledge the exact inspected/editable draft; changed drafts require a fresh review."""
    manifest = AdventureManifest.model_validate(
        value.model_dump() if isinstance(value, AdventureManifest) else value
    )
    validate_spans(extraction, manifest, degraded_page_budget=degraded_page_budget)
    if reviewed_digest != manifest_digest(manifest):
        raise ManifestError("The inspected manifest changed. Inspect it again before publishing.")
    manifest.reviewed_sha256 = reviewed_digest
    manifest.reviewed_by = reviewer
    return require_published(manifest)


def manifest_chunks(
    extraction: Extraction, value: dict | AdventureManifest, *,
    degraded_page_budget: float | None = None,
) -> list[dict]:
    """Materialize reviewed spans for the existing embedding pipeline, retaining all identities."""
    manifest = require_published(value)
    validate_spans(extraction, manifest, degraded_page_budget=degraded_page_budget)
    pages = dict(extraction.text_pages())
    chunks = []
    for index, passage in enumerate(manifest.passages):
        chunks.append(
            {
                "text": pages[passage.page][passage.start : passage.end],
                "source": manifest.source,
                "page": passage.page,
                "page_end": passage.page,
                "source_pages": [passage.page],
                "index": index,
                "source_sha256": manifest.source_sha256,
                "extraction_sha256": manifest.extraction_sha256,
                "rules_edition": manifest.rules_edition,
                "manifest_sha256": manifest.reviewed_sha256,
                "passage_id": passage.passage_id,
                "scene_id": passage.scene_ids[0] if len(passage.scene_ids) == 1 else None,
                "scene_ids": passage.scene_ids,
                "entity_ids": passage.entity_ids,
                "audience": passage.audience,
                "requires": passage.requires,
                "span_start": passage.start,
                "span_end": passage.end,
                "text_sha256": passage.text_sha256,
            }
        )
    return chunks
