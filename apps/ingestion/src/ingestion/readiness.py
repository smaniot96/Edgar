"""Persist inspectable extraction evidence and gate indexing before vector writes.

This proves extraction and index coverage, not a reviewed opening, encounter semantics or
atomic index promotion. Reports and Markdown contain local source text and stay in data storage.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal, NotRequired, Protocol, TypedDict

from edgar_core.sources import SourceEdition
from ingestion.artifacts import ExcludedPage, TextCorrection, corrections_digest, exclusions_digest
from ingestion.quality import diagnostic_message

REPORT_VERSION = 1
SampleKind = Literal["text", "stat", "scene_candidate"]


class ExtractionPage(Protocol):
    """The page evidence needed to decide whether an extraction can be indexed."""

    @property
    def page(self) -> int: ...

    @property
    def text(self) -> str: ...

    @property
    def method(self) -> str: ...

    @property
    def warnings(self) -> Sequence[str]: ...

    def errors(self) -> list[str]: ...


class ExtractionForReadiness(Protocol):
    """Structural contract shared by PDF and generated manuscript extraction."""

    @property
    def page_count(self) -> int: ...

    @property
    def pages(self) -> Sequence[ExtractionPage]: ...

    @property
    def rules_edition(self) -> SourceEdition: ...

    @property
    def excluded_pages(self) -> list[ExcludedPage]: ...

    @property
    def text_corrections(self) -> list[TextCorrection]: ...

    @property
    def source_sha256(self) -> str: ...

    def text_pages(self) -> list[tuple[int, str]]: ...

    def text_sha256(self) -> str: ...


class ReadinessIssue(TypedDict):
    code: str
    message: str
    detail_code: NotRequired[str]
    page: NotRequired[int]


class ReadinessChunk(TypedDict):
    page: int
    text: str
    source_pages: NotRequired[list[int]]
    stat_block: NotRequired[bool]
    page_end: NotRequired[int]
    section: NotRequired[str]
    source_sha256: NotRequired[str]
    extraction_sha256: NotRequired[str]
    rules_edition: NotRequired[SourceEdition]
    excluded_pages: NotRequired[list[ExcludedPage]]
    exclusions_sha256: NotRequired[str]
    text_corrections: NotRequired[list[TextCorrection]]
    corrections_sha256: NotRequired[str]
    corrected_pages: NotRequired[list[int]]


class ReadinessPreview(TypedDict, total=False):
    kind: SampleKind
    page: int
    page_end: int
    section: str | None
    text: str


class ReadinessReport(TypedDict):
    report_version: int
    source_kind: Literal["pdf", "generated", "text"]
    rules_edition: SourceEdition
    source_sha256: str
    extraction_sha256: str
    page_count: int
    required_pages: list[int]
    covered_pages: list[int]
    excluded_pages: NotRequired[list[ExcludedPage]]
    exclusions_sha256: NotRequired[str]
    text_corrections: NotRequired[list[TextCorrection]]
    corrections_sha256: NotRequired[str]
    chunk_count: int
    quality_passed: bool
    warning_count: int
    review_note: str
    issues: list[ReadinessIssue]
    previews: list[ReadinessPreview]
    scene_preview_reviewed: Literal[False]
    index_version: NotRequired[str]


class ReadinessSummary(TypedDict):
    report_version: int
    source_kind: Literal["pdf", "generated", "text"]
    rules_edition: SourceEdition
    source_sha256: str
    extraction_sha256: str
    page_count: int
    required_pages: list[int]
    excluded_pages: NotRequired[list[ExcludedPage]]
    exclusions_sha256: NotRequired[str]
    text_corrections: NotRequired[list[TextCorrection]]
    corrections_sha256: NotRequired[str]
    chunk_count: int
    quality_passed: bool
    issues: list[ReadinessIssue]
    warning_count: int
    review_note: str
    index_version: NotRequired[str]


class IngestionRejected(ValueError):
    """A safe, actionable failure whose extraction report remains inspectable."""

    def __init__(self, code: str, message: str, report: ReadinessReport):
        super().__init__(message)
        self.code = code
        self.report = report


def prepare_report(
    extraction: ExtractionForReadiness,
    *,
    source_kind: Literal["pdf", "generated", "text"],
    chunks: Sequence[ReadinessChunk],
    extra_issues: Sequence[ReadinessIssue] | None = None,
) -> ReadinessReport:
    """Describe the exact extraction and chunk coverage, including rejected output."""
    pages = extraction.text_pages()
    page_count = extraction.page_count
    excluded = {int(entry["page"]) for entry in extraction.excluded_pages}
    issues: list[ReadinessIssue] = list(extra_issues or [])
    numbers = [n for n, _ in pages]
    expected = [n for n in range(1, page_count + 1) if n not in excluded]
    if (
        (source_kind in ("pdf", "generated") and numbers != expected)
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
    for chunk in chunks:
        # Citations may never point into an excluded page, even via a multi-page range.
        cited = set(_cited_pages(chunk)) & excluded
        if cited:
            issues.append(
                {
                    "code": "excluded_page_cited",
                    "page": min(cited),
                    "message": "A chunk cites a page excluded from indexing. Re-review the exclusion list and chunking.",
                }
            )
            break
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
        if detail.page in excluded:
            # Reviewed as non-content; its diagnostics stay in the bundle, not as blockers.
            continue
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
    samples: list[ReadinessPreview] = []
    samples_to_preview: tuple[tuple[SampleKind, ReadinessChunk | None], ...] = (
        ("text", next((c for c in chunks if not c.get("stat_block")), None)),
        ("stat", next((c for c in chunks if c.get("stat_block")), None)),
        (
            "scene_candidate",
            next((c for c in chunks if c.get("section") and not c.get("stat_block")), None),
        ),
    )
    for kind, candidate in samples_to_preview:
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
    report: ReadinessReport = {
        "report_version": REPORT_VERSION,
        "source_kind": source_kind,
        "rules_edition": extraction.rules_edition,
        "source_sha256": extraction.source_sha256,
        "extraction_sha256": extraction.text_sha256(),
        "page_count": page_count,
        "required_pages": required_pages,
        "covered_pages": covered_pages,
        "excluded_pages": [
            ExcludedPage(page=e["page"], reason=e["reason"]) for e in extraction.excluded_pages
        ],
        "exclusions_sha256": exclusions_digest(extraction.excluded_pages),
        "chunk_count": len(chunks),
        "quality_passed": not issues,
        "warning_count": sum(len(p.warnings) for p in extraction.pages),
        "review_note": "Automatic checks cover extraction defects and index coverage. Inspect OCR, numbers and tables locally; scene candidates are not reviewed openings.",
        "issues": issues,
        "previews": samples,
        "scene_preview_reviewed": False,
    }
    if extraction.text_corrections:
        # Reviewed corrections are reported only when present; ordinary reports keep their shape.
        report["text_corrections"] = [TextCorrection(**c) for c in extraction.text_corrections]
        report["corrections_sha256"] = corrections_digest(extraction.text_corrections)
    return report


def report_summary(report: ReadinessReport) -> ReadinessSummary:
    """Small metadata record used for uncached live source verification."""
    summary: ReadinessSummary = {
        "report_version": report["report_version"],
        "source_kind": report["source_kind"],
        "rules_edition": report["rules_edition"],
        "source_sha256": report["source_sha256"],
        "extraction_sha256": report["extraction_sha256"],
        "page_count": report["page_count"],
        "required_pages": report["required_pages"],
        "chunk_count": report["chunk_count"],
        "quality_passed": report["quality_passed"],
        "issues": report["issues"],
        "warning_count": report["warning_count"],
        "review_note": report["review_note"],
    }
    # Reports persisted before reviewed exclusions existed carry no exclusion fields.
    if "excluded_pages" in report:
        summary["excluded_pages"] = report["excluded_pages"]
    if "exclusions_sha256" in report:
        summary["exclusions_sha256"] = report["exclusions_sha256"]
    if "corrections_sha256" in report:
        summary["text_corrections"] = report["text_corrections"]
        summary["corrections_sha256"] = report["corrections_sha256"]
    if report.get("index_version"):
        summary["index_version"] = report["index_version"]
    return summary


def require_ready(report: ReadinessReport) -> None:
    if not report["quality_passed"]:
        first = report["issues"][0]
        where = f" on page {first['page']}" if first.get("page") else ""
        raise IngestionRejected(
            "extraction_quality",
            f"Extraction needs review{where}: {first['message']} Inspect the extraction preview, then retry or upload a corrected PDF.",
            report,
        )


def _cited_pages(chunk: ReadinessChunk) -> list[int]:
    """Every physical page a chunk's text or citation may draw from."""
    first = chunk["page"]
    last = chunk.get("page_end", first)
    return sorted({*chunk.get("source_pages", []), *range(first, last + 1)})


def stamp_chunks(chunks: Sequence[ReadinessChunk], report: ReadinessReport) -> None:
    for chunk in chunks:
        chunk["source_sha256"] = report["source_sha256"]
        chunk["extraction_sha256"] = report["extraction_sha256"]
        chunk["rules_edition"] = report["rules_edition"]
        if report.get("excluded_pages"):
            # Only reviewed exclusions change index identity; ordinary bundles keep theirs.
            chunk["excluded_pages"] = report["excluded_pages"]
            chunk["exclusions_sha256"] = report["exclusions_sha256"]
        if report.get("text_corrections"):
            # Corrections are stamped like exclusions; each chunk also names its own corrected pages.
            chunk["text_corrections"] = report["text_corrections"]
            chunk["corrections_sha256"] = report["corrections_sha256"]
            corrected = {c["page"] for c in report["text_corrections"]}
            cited = sorted(corrected & set(_cited_pages(chunk)))
            if cited:
                chunk["corrected_pages"] = cited
