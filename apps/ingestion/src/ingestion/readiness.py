"""Persist inspectable extraction evidence and gate indexing before vector writes.

This proves extraction and index coverage, not a reviewed opening, encounter semantics or
atomic index promotion. Reports and Markdown contain local source text and stay in data storage.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ingestion.quality import diagnostic_message

if TYPE_CHECKING:
    from ingestion.extract import Extraction

REPORT_VERSION = 1


class IngestionRejected(ValueError):
    """A safe, actionable failure whose extraction report remains inspectable."""

    def __init__(self, code: str, message: str, report: dict):
        super().__init__(message)
        self.code = code
        self.report = report


def prepare_report(
    extraction: Extraction,
    *,
    source_kind: str,
    chunks: list[dict],
    extra_issues: list[dict] | None = None,
) -> dict:
    """Describe the exact extraction and chunk coverage, including rejected output."""
    pages = extraction.text_pages()
    page_count = extraction.page_count
    issues = list(extra_issues or [])
    numbers = [n for n, _ in pages]
    if (
        (source_kind in ("pdf", "generated") and numbers != list(range(1, page_count + 1)))
        or len(set(numbers)) != len(numbers)
        or any(n < 1 for n in numbers)
    ):
        issues.append(
            {
                "code": "source_page_coverage",
                "message": "Extraction must cover every source page in order.",
            }
        )
    required_pages = [n for n, text in pages if text.strip()]
    covered_pages = sorted({n for c in chunks for n in c.get("source_pages", [])})
    if not required_pages or not chunks:
        issues.append(
            {"code": "empty_extraction", "message": "No usable source text was extracted."}
        )
    if not set(required_pages).issubset(covered_pages):
        issues.append(
            {
                "code": "chunk_page_coverage",
                "message": "Some extracted source pages were lost during chunking.",
            }
        )
    for detail in extraction.pages:
        for error in detail.errors():
            issues.append(
                {
                    "code": "extraction_quality",
                    "detail_code": error,
                    "page": detail.page,
                    "message": diagnostic_message(error),
                }
            )
        # Blank physical pages are explicitly classified by extraction. Empty output from a
        # content-bearing/OCR page must not disappear silently from the required coverage.
        if not detail.text.strip() and detail.method != "blank_page":
            issues.append(
                {
                    "code": "empty_content_page",
                    "page": detail.page,
                    "message": "A source page produced no text. Inspect OCR and retry with a readable PDF.",
                }
            )
    samples = []
    for kind, candidate in (
        ("text", next((c for c in chunks if not c.get("stat_block")), None)),
        ("stat", next((c for c in chunks if c.get("stat_block")), None)),
        (
            "scene_candidate",
            next((c for c in chunks if c.get("section") and not c.get("stat_block")), None),
        ),
    ):
        if candidate is not None:
            samples.append(
                {
                    "kind": kind,
                    "page": candidate["page"],
                    "page_end": candidate.get("page_end", candidate["page"]),
                    "section": candidate.get("section"),
                    "text": candidate["text"][:2000],
                }
            )
    return {
        "report_version": REPORT_VERSION,
        "source_kind": source_kind,
        "rules_edition": extraction.rules_edition,
        "source_sha256": extraction.source_sha256,
        "extraction_sha256": extraction.text_sha256(),
        "page_count": page_count,
        "required_pages": required_pages,
        "covered_pages": covered_pages,
        "chunk_count": len(chunks),
        "quality_passed": not issues,
        "warning_count": sum(len(p.warnings) for p in extraction.pages),
        "review_note": "Automatic checks cover extraction defects and index coverage. Inspect OCR, numbers and tables locally; scene candidates are not reviewed openings.",
        "issues": issues,
        "previews": samples,
        "scene_preview_reviewed": False,
    }


def report_summary(report: dict) -> dict:
    """Small metadata record used for uncached live source verification."""
    return {
        key: report[key]
        for key in (
            "report_version",
            "source_kind",
            "rules_edition",
            "source_sha256",
            "extraction_sha256",
            "page_count",
            "required_pages",
            "chunk_count",
            "quality_passed",
            "issues",
            "warning_count",
            "review_note",
        )
    }


def require_ready(report: dict) -> None:
    if not report["quality_passed"]:
        first = report["issues"][0]
        where = f" on page {first['page']}" if first.get("page") else ""
        raise IngestionRejected(
            "extraction_quality",
            f"Extraction needs review{where}: {first['message']} Inspect the extraction preview, then retry or upload a corrected PDF.",
            report,
        )


def stamp_chunks(chunks: list[dict], report: dict) -> None:
    for chunk in chunks:
        chunk["source_sha256"] = report["source_sha256"]
        chunk["extraction_sha256"] = report["extraction_sha256"]
        chunk["rules_edition"] = report["rules_edition"]
