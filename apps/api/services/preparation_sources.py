"""Retain exact preparation inputs without activating or changing any source index."""

from __future__ import annotations

import shutil
from pathlib import Path

from edgar_core.artifacts import text_digest
from edgar_core.preparation import PreparationRunBinding
from ingestion.extract import (
    Extraction,
    extract_document,
    load_reviewed_extraction,
    save_extraction,
)
from ingestion.readiness import prepare_report, require_ready


def source_directory(slug: str, run_id: str) -> Path:
    from .preparation_store import preparation_path

    return preparation_path(slug, run_id).parent / "source"


def retain_pdf(slug: str, run_id: str, pdf_path: str | Path, edition: str) -> Extraction:
    """Extract once from an immutable candidate copy, before commissioning model work."""
    directory = source_directory(slug, run_id)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{slug}.pdf"
    if target.exists():
        raise ValueError("Preparation source already exists; use its frozen run identity.")
    shutil.copyfile(pdf_path, target)
    extraction = extract_document(target)
    extraction.rules_edition = edition
    from ingestion.chunk import chunk_pages

    report = prepare_report(
        extraction, source_kind="pdf",
        chunks=chunk_pages(extraction.text_pages(), source=slug),
    )
    save_extraction(extraction, directory, readiness=report)
    require_ready(report)
    return extraction


def load_source(binding: PreparationRunBinding) -> Extraction:
    directory = source_directory(binding.source_slug, binding.run_id)
    extraction = load_reviewed_extraction(
        directory / f"{binding.source_slug}.pdf" if binding.source_mode == "pdf" else None,
        directory / f"{binding.source_slug}.extraction.json",
    )
    if extraction.rules_edition != binding.rules_edition:
        raise ValueError("Frozen source edition changed.")
    if binding.source_mode == "pdf" and (
        extraction.source_sha256 != binding.source_sha256
        or extraction.text_sha256() != binding.extraction_sha256
    ):
        raise ValueError("Frozen PDF source or extraction changed.")
    return extraction


def retain_manuscript(binding: PreparationRunBinding, extraction: Extraction) -> dict:
    """Save the complete AI manuscript or the separate PDF DM guide privately.

    An AI source uses its retained complete text identity. A PDF guide never replaces
    the physical source extraction and never claims extra printed pages.
    """
    directory = source_directory(binding.source_slug, binding.run_id)
    if binding.source_mode == "pdf":
        guide = directory / "private-dm-guide.md"
        text = extraction.markdown()
        guide.write_text(text, encoding="utf-8")
        return {"manuscript_sha256": text_digest(text), "kind": "dm_guide"}
    if extraction.source != binding.source_slug or extraction.rules_edition != binding.rules_edition:
        raise ValueError("Manuscript does not match its run source and edition.")
    if extraction.source_sha256 != extraction.text_sha256():
        raise ValueError("Generated source identity must be the exact retained manuscript digest.")
    from ingestion.chunk import chunk_pages

    report = prepare_report(
        extraction, source_kind="generated",
        chunks=chunk_pages(extraction.text_pages(), source=binding.source_slug),
    )
    require_ready(report)
    save_extraction(extraction, directory, readiness=report)
    return {"manuscript_sha256": extraction.text_sha256(), "kind": "generated"}
