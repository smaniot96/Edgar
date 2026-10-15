"""Per-page native/OCR selection, diagnostics and inspectable extraction bundles.

Page numbers are physical, one-based PDF pages, unchanged from the original contract.
No PDF, index or database is mutated by extraction or by saving a preview.
"""

import json
import re
import subprocess
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

import pymupdf
import pymupdf4llm

from edgar_core.sources import SourceEdition
from ingestion.artifacts import source_digest, text_digest
from ingestion.layout import native_lines, ocr_stat_header, render_native_page
from ingestion.quality import check_text

EXTRACTION_VERSION = 1


@dataclass
class ExtractedPage:
    page: int
    text: str
    method: str
    quality: dict
    warnings: list[str] = field(default_factory=list)
    candidates: list[dict] = field(default_factory=list)

    def errors(self) -> list[str]:
        """Recheck selected text while retaining geometry/OCR failures from extraction."""
        return sorted(set(self.quality.get("errors", []) + check_text(self.text)["errors"]))


@dataclass
class Extraction:
    source: str
    source_sha256: str
    page_count: int
    pages: list[ExtractedPage]
    rules_edition: SourceEdition = "unspecified"

    def text_pages(self) -> list[tuple[int, str]]:
        return [(p.page, p.text) for p in self.pages]

    def markdown(self) -> str:
        return "\n\n".join(f"<!-- PDF page {p.page} -->\n\n{p.text}" for p in self.pages)

    def text_sha256(self) -> str:
        return text_digest(self.markdown())

    def require_valid(self) -> None:
        if self.rules_edition not in ("2014", "2024", "unspecified"):
            raise ValueError("Declare rules edition 2014, 2024 or unspecified.")
        errors = {p.page: errors for p in self.pages if (errors := p.errors())}
        if errors:
            raise ValueError(
                f"Extraction quality checks failed: {errors}. Save and inspect the preview before indexing."
            )


def extract_document(path: str | Path, *, pages: list[int] | None = None) -> Extraction:
    """Extract once; return selected text and rejected-candidate reasons for review.

    A substantial native layer is ordered geometrically. Missing raster stat headers are
    supplemented locally. Sparse native pages fall back to explicit forced OCR; OCR must
    pass the same text checks. Ambiguous geometry fails closed and remains inspectable.
    `pages` is a physical, one-based subset intended for local QA, never full ingestion.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"PDF not found: {path}")
    extracted = []
    with pymupdf.open(path) as doc:
        selected = list(range(1, len(doc) + 1)) if pages is None else pages
        if len(set(selected)) != len(selected) or any(n < 1 or n > len(doc) for n in selected):
            raise ValueError("Pages must be unique physical PDF page numbers within the document.")
        for number in sorted(selected):
            page = doc[number - 1]
            lines, duplicates = native_lines(page.get_text("dict"), page.rect.height)
            native_characters = sum(len(item.text) for item in lines)
            warnings, candidates = [], []
            text, method, fatal = "", "native_geometry", []
            blank_page = not lines and not page.get_images() and not page.get_drawings()
            if blank_page:
                method = "blank_page"
            elif native_characters >= 300:
                try:
                    text, warnings = render_native_page(
                        lines,
                        page.rect.width,
                        header_ocr=lambda bbox, source_page=page: ocr_stat_header(
                            source_page, bbox
                        ),
                    )
                    if warnings:
                        method = "native_geometry+raster_headers"
                    if duplicates:
                        warnings.append(f"removed_identical_overlapping_spans:{duplicates}")
                except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
                    # Retain the native material for diagnosis; never index ambiguous output.
                    text = "\n".join(item.text for item in lines)
                    fatal.append(str(exc))
            else:
                method = "layout_ocr"
                candidates.append(
                    {
                        "method": "native_geometry",
                        "characters": native_characters,
                        "rejected": "insufficient_native_text",
                    }
                )
                try:
                    text = pymupdf4llm.to_markdown(
                        doc,
                        pages=[number - 1],
                        page_chunks=False,
                        header=False,
                        footer=False,
                        use_ocr=True,
                        force_ocr=True,
                    )
                    warnings.append("full_page_ocr_review_required")
                    # A sparse native layer may still contain authoritative numbers.
                    # Require those to survive OCR; never silently overwrite them.
                    native_numbers = Counter(
                        re.findall(r"\b\d+(?:d\d+)?\b", " ".join(item.text for item in lines))
                    )
                    ocr_numbers = Counter(re.findall(r"\b\d+(?:d\d+)?\b", text))
                    if native_numbers - ocr_numbers:
                        fatal.append("partial_native_numbers_lost_in_ocr")
                    if len(re.sub(r"\W", "", text)) < 40:
                        fatal.append("insufficient_ocr_text_on_nonblank_page")
                except Exception as exc:
                    fatal.append(
                        f"OCR unavailable or failed ({type(exc).__name__}); install Tesseract with English language data and retry"
                    )
            quality = check_text(text)
            quality["blank_page"] = blank_page
            quality["native_characters"] = native_characters
            quality["removed_overlapping_spans"] = duplicates
            quality["errors"] = sorted(set(quality["errors"] + fatal))
            if page.get_images() and native_characters >= 300:
                warnings.append("raster_backed_native_layer_completeness_review_required")
            if re.search(r"\b[lI]\d|\b\d+[lI]|\b(?:GO|[lI]d\d+)\b", text):
                warnings.append("source_text_possible_numeric_ocr_ambiguity")
            extracted.append(ExtractedPage(number, text, method, quality, warnings, candidates))
        return Extraction(path.stem, source_digest(path), len(doc), extracted)


def save_extraction(
    extraction: Extraction,
    output_dir: str | Path,
    *,
    readiness: dict | None = None,
) -> tuple[Path, Path]:
    """Save selected text and diagnostics from the SAME run; no second extraction."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    markdown = output_dir / f"{extraction.source}.md"
    report = output_dir / f"{extraction.source}.extraction.json"
    text = extraction.markdown()
    markdown.write_text(text, encoding="utf-8")
    data = asdict(extraction)
    data.update(
        {
            "extraction_version": EXTRACTION_VERSION,
            "parser_version": pymupdf4llm.__version__,
            "markdown_sha256": extraction.text_sha256(),
        }
    )
    if readiness is not None:
        data["readiness"] = readiness
    for page in data["pages"]:
        page["text_sha256"] = text_digest(page["text"])
    report.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return markdown, report


def load_reviewed_extraction(pdf_path: str | Path | None, report_path: str | Path) -> Extraction:
    """Use the exact preview explicitly acknowledged by the caller, without re-extracting.

    Pass pdf_path=None only for an explicitly generated/text manuscript bundle.
    This is a local review contract, not authentication or atomic index promotion. Reject
    stale sources, incomplete subsets, changed previews and failed quality checks before
    any embedding/client call. The JSON's selected text is the indexing input.
    """
    pdf_path, report_path = Path(pdf_path) if pdf_path is not None else None, Path(report_path)
    data = json.loads(report_path.read_text(encoding="utf-8"))
    if data["extraction_version"] != EXTRACTION_VERSION:
        raise ValueError("Extraction preview version changed; extract and review again.")
    source = (
        pdf_path.stem if pdf_path is not None else report_path.name.removesuffix(".extraction.json")
    )
    if data["source"] != source:
        raise ValueError("Extraction preview does not match this source; extract and review again.")
    if pdf_path is not None:
        if data["source_sha256"] != source_digest(pdf_path):
            raise ValueError(
                "Extraction preview does not match this PDF; extract and review again."
            )
    elif (data.get("readiness") or {}).get("source_kind") not in ("generated", "text") or data[
        "source_sha256"
    ] != data["markdown_sha256"]:
        raise ValueError(
            "A generated review requires an explicit manuscript bundle and matching text identity."
        )
    markdown = report_path.with_name(f"{source}.md")
    if text_digest(markdown.read_text(encoding="utf-8")) != data["markdown_sha256"]:
        raise ValueError("Markdown preview changed; extract and review a consistent bundle again.")
    page_numbers = [p["page"] for p in data["pages"]]
    count = data["page_count"]
    if pdf_path is not None:
        with pymupdf.open(pdf_path) as doc:
            count = len(doc)
    if data["page_count"] != count or page_numbers != list(range(1, count + 1)):
        raise ValueError(
            "Review bundle must cover every physical PDF page in order."
            if pdf_path is not None
            else "Review bundle must cover every manuscript page in order."
        )
    pages = []
    for page in data["pages"]:
        if page["text_sha256"] != text_digest(page["text"]):
            raise ValueError("Selected extraction text changed; extract and review again.")
        pages.append(
            ExtractedPage(
                **{
                    key: page[key]
                    for key in ("page", "text", "method", "quality", "warnings", "candidates")
                }
            )
        )
    extraction = Extraction(
        data["source"],
        data["source_sha256"],
        data["page_count"],
        pages,
        rules_edition=data.get("rules_edition", "unspecified"),
    )
    if extraction.text_sha256() != data["markdown_sha256"]:
        raise ValueError("Selected text differs from the reviewed Markdown preview.")
    extraction.require_valid()
    return extraction
