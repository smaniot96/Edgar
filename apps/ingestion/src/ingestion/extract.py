"""Per-page native/OCR selection, diagnostics and inspectable extraction bundles.

Page numbers are physical, one-based PDF pages, unchanged from the original contract.
No PDF, index or database is mutated by extraction or by saving a preview.
"""

import csv
import io
import json
import re
import shutil
import subprocess
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path

import pymupdf
import pymupdf4llm

from edgar_core.sources import SourceEdition
from ingestion.artifacts import ExcludedPage, exclusions_digest, source_digest, text_digest
from ingestion.layout import Line, native_lines, ocr_stat_header, render_native_page
from ingestion.quality import check_text, diagnostic_message, has_section_heading, has_stat_record
from ingestion.text_quality import is_degraded, prefer_ocr, score_text

EXTRACTION_VERSION = 1
SUMMARY_PAGE_LIMIT = 20
EXCLUSION_REASONS = ("cover", "credits", "art", "form", "index")
# Tesseract at the 600 dpi used for raster stat headings in layout.ocr_stat_header.
OCR_LAYOUT_DPI = 600
OCR_MIN_CONFIDENCE = 30


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
    excluded_pages: list[ExcludedPage] = field(default_factory=list)

    def excluded_numbers(self) -> set[int]:
        return {int(entry["page"]) for entry in self.excluded_pages}

    def text_pages(self) -> list[tuple[int, str]]:
        """Pages eligible for chunking; reviewed exclusions are never chunked or cited."""
        excluded = self.excluded_numbers()
        return [(p.page, p.text) for p in self.pages if p.page not in excluded]

    def markdown(self) -> str:
        # Exclusions are part of the reviewed preview, so changing them changes its digest.
        reasons = {int(entry["page"]): entry["reason"] for entry in self.excluded_pages}
        blocks = []
        for p in self.pages:
            marker = f"<!-- PDF page {p.page} -->"
            if p.page in reasons:
                marker = f"<!-- PDF page {p.page} excluded: {reasons[p.page]} (not indexed) -->"
            blocks.append(f"{marker}\n\n{p.text}")
        return "\n\n".join(blocks)

    def text_sha256(self) -> str:
        return text_digest(self.markdown())

    def failing_pages(self) -> dict[int, list[str]]:
        # Excluded pages are reviewed as non-content; every other page is still checked.
        excluded = self.excluded_numbers()
        return {
            p.page: errors for p in self.pages if p.page not in excluded and (errors := p.errors())
        }

    def quality_summary(self) -> list[str]:
        """Bounded, text-free lines: failing pages with codes, then totals per code."""
        failing = self.failing_pages()
        totals = Counter(code for codes in failing.values() for code in codes)
        lines = [f"{len(failing)} of {self.page_count} pages failed quality checks."]
        for page, codes in list(failing.items())[:SUMMARY_PAGE_LIMIT]:
            details = "; ".join(f"{code} ({diagnostic_message(code)})" for code in codes)
            lines.append(f"page {page}: {details}")
        if len(failing) > SUMMARY_PAGE_LIMIT:
            lines.append(
                f"... {len(failing) - SUMMARY_PAGE_LIMIT} more pages; see the extraction bundle."
            )
        if self.excluded_pages:
            listed = [f"page {e['page']} ({e['reason']})" for e in self.excluded_pages]
            lines.append(
                f"Excluded from indexing ({len(listed)}): "
                + ", ".join(listed[:SUMMARY_PAGE_LIMIT])
                + ("; more in the extraction bundle" if len(listed) > SUMMARY_PAGE_LIMIT else "")
            )
            for entry in self.excluded_pages:
                excluded = next(p for p in self.pages if p.page == entry["page"])
                if has_section_heading(excluded.text):
                    lines.append(
                        f"page {entry['page']}: excluded page has a section heading; confirm it is not rules text."
                    )
        lines.append(
            "Totals: " + (", ".join(f"{c} x{n}" for c, n in sorted(totals.items())) or "none")
        )
        return lines

    def require_valid(self) -> None:
        if self.rules_edition not in ("2014", "2024", "unspecified"):
            raise ValueError("Declare rules edition 2014, 2024 or unspecified.")
        errors = self.failing_pages()
        if errors:
            raise ValueError(
                f"Extraction quality checks failed: {errors}. Save and inspect the preview before indexing."
            )


def validate_exclusions(
    entries: Sequence[object], pages: list[ExtractedPage], page_count: int
) -> list[ExcludedPage]:
    """Reviewed non-content pages: known reasons, extracted pages, never a stat record."""
    by_number = {p.page: p for p in pages}
    seen: set[int] = set()
    cleaned: list[ExcludedPage] = []
    for entry in entries:
        if (
            not isinstance(entry, dict)
            or set(entry) != {"page", "reason"}
            or type(entry["page"]) is not int
        ):
            raise ValueError("Each excluded page needs a physical page number and a reason.")
        number: int = entry["page"]
        reason = entry["reason"]
        if reason not in EXCLUSION_REASONS:
            raise ValueError(f"Exclusion reason must be one of: {', '.join(EXCLUSION_REASONS)}.")
        if not 1 <= number <= page_count or number not in by_number:
            raise ValueError(f"Excluded page {number} is not an extracted physical PDF page.")
        if number in seen:
            raise ValueError(f"Page {number} is excluded more than once.")
        if has_stat_record(by_number[number].text):
            raise ValueError(f"Page {number} contains a stat record and cannot be excluded.")
        seen.add(number)
        cleaned.append({"page": number, "reason": str(reason)})
    return sorted(cleaned, key=lambda e: e["page"])


def _ocr_page(doc: pymupdf.Document, number: int) -> str:
    """Full-page Tesseract read through the same route for sparse and degraded pages."""
    return pymupdf4llm.to_markdown(
        doc,
        pages=[number - 1],
        page_chunks=False,
        header=False,
        footer=False,
        use_ocr=True,
        force_ocr=True,
    )


def _tesseract_page_text(page: pymupdf.Page) -> str:
    """Tesseract word boxes at the layout module's raster resolution, in native geometry order.

    The page is read as one piece, so native and OCR text never mix inside a stat block. When
    the geometry is ambiguous, Tesseract's own block order is kept instead; the caller's text
    checks and numeric comparison still decide whether that OCR text may replace the native layer.
    """
    binary = shutil.which("tesseract")
    if binary is None:
        raise RuntimeError("Tesseract with English language data is required for OCR.")
    pix = page.get_pixmap(dpi=OCR_LAYOUT_DPI)
    process = subprocess.run(
        [binary, "stdin", "stdout", "-l", "eng", "--psm", "3", "tsv"],
        input=pix.tobytes("png"),
        capture_output=True,
        timeout=120,
        check=True,
    )
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for word in csv.DictReader(io.StringIO(process.stdout.decode()), delimiter="\t"):
        if word["text"].strip() and float(word["conf"]) >= OCR_MIN_CONFIDENCE:
            groups[tuple(word[k] for k in ("block_num", "par_num", "line_num"))].append(word)
    scale = 72 / OCR_LAYOUT_DPI
    lines = []
    for words in groups.values():
        words.sort(key=lambda w: int(w["left"]))
        x0 = min(int(w["left"]) for w in words)
        y0 = min(int(w["top"]) for w in words)
        x1 = max(int(w["left"]) + int(w["width"]) for w in words)
        y1 = max(int(w["top"]) + int(w["height"]) for w in words)
        size = (y1 - y0) * scale / 1.2
        bbox = (x0 * scale, y0 * scale, x1 * scale, y1 * scale)
        lines.append(Line(" ".join(w["text"] for w in words), bbox, size))
    try:
        text, _warnings = render_native_page(lines, page.rect.width)
    except ValueError:
        text = "\n".join(line.text for line in lines)
    return text


def _prefer_fresh_ocr(
    page: pymupdf.Page,
    lines: list[Line],
    native_text: str,
    method: str,
    scores: dict,
    warnings: list[str],
    candidates: list[dict],
) -> tuple[str, str]:
    """Replace a degraded native layer with fresh OCR only when OCR is better and passes checks.

    The whole page switches source; native and OCR text are never mixed inside a stat block.
    Both signature scores are recorded in the bundle whichever source is kept.
    """
    native_scores = scores["native_geometry"]
    try:
        ocr_text = _tesseract_page_text(page)
    except Exception as exc:
        warnings.append("degraded_native_text_layer_ocr_unavailable")
        candidates.append({"method": "layout_ocr", "rejected": f"unavailable:{type(exc).__name__}"})
        return native_text, method
    ocr_scores = score_text(ocr_text)
    scores["layout_ocr"] = ocr_scores
    native_numbers = Counter(re.findall(r"\b\d+(?:d\d+)?\b", " ".join(item.text for item in lines)))
    ocr_numbers = Counter(re.findall(r"\b\d+(?:d\d+)?\b", ocr_text))
    reasons = []
    if check_text(ocr_text)["errors"]:
        reasons.append("ocr_failed_text_checks")
    if native_numbers - ocr_numbers:
        reasons.append("ocr_dropped_native_numbers")
    if not prefer_ocr(native_scores, ocr_scores):
        reasons.append("ocr_not_better")
    if reasons:
        warnings.append("degraded_native_text_layer_kept")
        candidates.append({"method": "layout_ocr", "rejected": ",".join(reasons), **ocr_scores})
        return native_text, method
    # Warnings about the discarded native render (raster headers, spans) no longer apply.
    warnings.clear()
    warnings.append("degraded_native_text_layer_replaced_with_ocr")
    warnings.append("full_page_ocr_review_required")
    candidates.append({"method": method, "rejected": "degraded_signature", **native_scores})
    return ocr_text, "layout_ocr"


def extract_document(path: str | Path, *, pages: list[int] | None = None) -> Extraction:
    """Extract once; return selected text and rejected-candidate reasons for review.

    A substantial native layer is ordered geometrically. Missing raster stat headers are
    supplemented locally. A native layer with a dense OCR-misreading signature (see
    `ingestion.text_quality`) is compared with fresh OCR and the better whole page is kept.
    Sparse native pages fall back to explicit forced OCR; OCR must pass the same text checks.
    Ambiguous geometry fails closed and remains inspectable.
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
            warnings, candidates, scores = [], [], {}
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
                scores["native_geometry"] = score_text(text)
                if not fatal and is_degraded(scores["native_geometry"]):
                    text, method = _prefer_fresh_ocr(
                        page, lines, text, method, scores, warnings, candidates
                    )
            else:
                method = "layout_ocr"
                candidates.append(
                    {
                        "method": "native_geometry",
                        "characters": native_characters,
                        "rejected": "insufficient_native_text",
                    }
                )
                scores["native_geometry"] = score_text("\n".join(item.text for item in lines))
                try:
                    text = _ocr_page(doc, number)
                    scores["layout_ocr"] = score_text(text)
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
            # Both candidate sources are recorded; the selected text is the page's method.
            quality["source_scores"] = scores
            if score_text(text)["signature_hits"]:
                warnings.append("ocr_signature_review_required")
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
    name: str | None = None,
) -> tuple[Path, Path]:
    """Save selected text and diagnostics from the SAME run; no second extraction.

    `name` overrides the file stem only, e.g. `<source>.failed` keeps a failed run beside
    the previous preview. The bundle's own `source` stays unchanged.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = name or extraction.source
    markdown = output_dir / f"{stem}.md"
    report = output_dir / f"{stem}.extraction.json"
    text = extraction.markdown()
    markdown.write_text(text, encoding="utf-8")
    data = asdict(extraction)
    data.update(
        {
            "extraction_version": EXTRACTION_VERSION,
            "parser_version": pymupdf4llm.__version__,
            "markdown_sha256": extraction.text_sha256(),
            "exclusions_sha256": exclusions_digest(extraction.excluded_pages),
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
    excluded = validate_exclusions(data.get("excluded_pages", []), pages, data["page_count"])
    if exclusions_digest(excluded) != data.get("exclusions_sha256", exclusions_digest([])):
        raise ValueError("Excluded pages changed; extract and review the bundle again.")
    extraction = Extraction(
        data["source"],
        data["source_sha256"],
        data["page_count"],
        pages,
        rules_edition=data.get("rules_edition", "unspecified"),
        excluded_pages=excluded,
    )
    if extraction.text_sha256() != data["markdown_sha256"]:
        raise ValueError("Selected text differs from the reviewed Markdown preview.")
    extraction.require_valid()
    return extraction
