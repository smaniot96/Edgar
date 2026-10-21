"""Index independently reviewed semantic records with exact span/coverage integrity.

These records can contain scene prose, several quoted statistics, procedures or private
original OCR. They are not newly extracted physical PDF pages, so PDF column/stat-heading
heuristics are inapplicable. Their acceptance boundary is the reviewed manifest and its
exact source, passage, classification and reference checksums.
"""

import json
from dataclasses import dataclass
from pathlib import Path

from db.vector.versions import index_version
from edgar_core.manifests import AdventureManifest, ManifestError, require_published
from ingestion.embed import _chunk_payload
from ingestion.extract import EXTRACTION_VERSION, ExtractedPage, Extraction, save_extraction
from ingestion.manifest import manifest_chunks
from ingestion.readiness import prepare_report, require_ready, stamp_chunks


class SemanticRecordPage(ExtractedPage):
    """A generated record, whose content is validated through exact reviewed spans."""

    def errors(self) -> list[str]:
        errors = []
        if not self.text.strip():
            errors.append("empty_semantic_record")
        if "\x00" in self.text:
            errors.append("invalid_semantic_record_text")
        return errors


@dataclass
class SemanticRecordExtraction(Extraction):
    document_kind: str = "reviewed-semantic-records-v1"
    reviewed_manifest: dict | None = None

    def markdown(self):
        # Keep this exact representation stable: existing paid review/source receipts
        # already cover these bytes, including records retained before the discriminator.
        return "\n\n".join(
            f"<!-- Authored campaign record {page.page}; not a PDF page -->\n\n{page.text}"
            for page in self.pages
        )


def prepare_reviewed_manifest_records(
    extraction: Extraction,
    value: dict | AdventureManifest,
    *,
    preview_dir: Path | None = None,
    on_prepared=None,
) -> list[dict]:
    """Prepare exact reviewed record chunks for the unchanged atomic index primitive.

    No exclusions, degraded-page allowance or PDF heuristic override participates. The
    reviewed manifest still proves complete, non-overlapping text classification, exact
    passage/source digests, valid references and a reviewed public opening. Readiness
    additionally proves ordered record coverage and nonempty chunkable content before any
    embedding or vector write. Ordinary extracted PDF pages keep their existing checks.
    """
    if (
        not isinstance(extraction, SemanticRecordExtraction)
        or not extraction.pages
        or any(not isinstance(page, SemanticRecordPage) for page in extraction.pages)
    ):
        raise ValueError("Semantic publication requires explicit semantic record pages.")
    if extraction.excluded_pages or extraction.text_corrections:
        raise ValueError("Semantic publication requires complete unmodified record text.")
    if extraction.source_sha256 != extraction.text_sha256():
        raise ManifestError("Semantic source checksum does not match its complete record text.")
    manifest = require_published(value)
    chunks = manifest_chunks(extraction, manifest)
    report = prepare_report(extraction, source_kind="generated", chunks=chunks)
    report["scene_preview_reviewed"] = True
    report["review_note"] = (
        "Independently reviewed semantic records: exact manifest/source/passage checksums, "
        "complete classified spans, valid references, public opening and record/chunk coverage. "
        "Original PDF OCR and layout diagnostics remain in the private source artifacts."
    )
    if report["quality_passed"]:
        stamp_chunks(chunks, report)
        report["index_version"] = index_version([_chunk_payload(chunk) for chunk in chunks])
    if preview_dir is not None:
        extraction.reviewed_manifest = manifest.model_dump(mode="json")
        save_extraction(extraction, preview_dir, readiness=report)
    if on_prepared is not None:
        on_prepared(report)
    require_ready(report)
    return chunks


def load_reviewed_manifest_records(
    report_path: str | Path,
    value: dict | AdventureManifest,
) -> SemanticRecordExtraction:
    """Reopen semantic artifacts with their explicit format and exact expected manifest.

    The ordinary PDF reader must never reconstruct these as PDF extraction pages. A
    saved semantic bundle carries its reviewed manifest, whose digest must also match
    the caller's publication receipt. Text, markdown, coverage and spans are rechecked.
    """
    report_path = Path(report_path)
    data = json.loads(report_path.read_text(encoding="utf-8"))
    manifest = require_published(value)
    saved = require_published(data.get("reviewed_manifest") or {})
    if (
        data.get("document_kind") != "reviewed-semantic-records-v1"
        or data.get("extraction_version") != EXTRACTION_VERSION
        or saved.reviewed_sha256 != manifest.reviewed_sha256
        or data.get("source") != manifest.source
    ):
        raise ManifestError("Saved semantic records do not match the exact reviewed manifest.")
    extraction = SemanticRecordExtraction(
        data["source"],
        data["source_sha256"],
        data["page_count"],
        [
            SemanticRecordPage(
                **{
                    key: page[key]
                    for key in ("page", "text", "method", "quality", "warnings", "candidates")
                }
            )
            for page in data["pages"]
        ],
        rules_edition=data["rules_edition"],
        excluded_pages=data.get("excluded_pages", []),
        text_corrections=data.get("text_corrections", []),
        reviewed_manifest=saved.model_dump(mode="json"),
    )
    from ingestion.artifacts import text_digest

    markdown = report_path.with_name(f"{manifest.source}.md").read_text(encoding="utf-8")
    if (
        text_digest(markdown) != extraction.text_sha256()
        or data.get("markdown_sha256") != extraction.text_sha256()
        or any(page.get("text_sha256") != text_digest(page["text"]) for page in data["pages"])
    ):
        raise ManifestError("Saved semantic record text changed after review.")
    prepare_reviewed_manifest_records(extraction, manifest)
    return extraction
