"""Native span reading order with narrowly scoped raster supplements.

The installed layout parser can discard valid native stat text, or merge native and OCR
layers. Work from the native coordinates instead. Raster OCR supplements only a missing
stat name/type; native mechanics are never replaced with OCR guesses.
"""

import csv
import io
import re
import shutil
import statistics
import subprocess
from dataclasses import dataclass

import pymupdf

from ingestion.quality import SIZE_TYPE, STAT_SUBHEADINGS, letters


@dataclass
class Line:
    text: str
    bbox: tuple[float, float, float, float]
    size: float = 0
    heading: bool = False


def native_lines(data: dict, height: float) -> tuple[list[Line], int]:
    """Keep source strings and geometry; remove only identical overlapping spans."""
    out = []
    seen: dict[str, list[tuple]] = {}
    duplicates = 0
    for block in data["blocks"]:
        for line in block.get("lines", []):
            spans = []
            for span in line["spans"]:
                text, bbox = span["text"], tuple(span["bbox"])
                if bbox[1] >= height * 0.95 or bbox[3] <= height * 0.025:
                    continue
                if text.strip() and any(
                    max(abs(a - b) for a, b in zip(bbox, other, strict=True)) <= 1.5
                    for other in seen.get(text, [])
                ):
                    duplicates += 1
                    continue
                if text.strip():
                    seen.setdefault(text, []).append(bbox)
                spans.append(span)
            if spans:
                text = "".join(s["text"] for s in spans).strip()
                if text:
                    out.append(Line(text, tuple(line["bbox"]), max(s["size"] for s in spans)))
    return out, duplicates


def columns(lines: list[Line], width: float) -> list[list[Line]]:
    """Infer a two-column gutter from line edges, otherwise retain a single column.

    Require substantial vertical overlap and a clear gutter. Wide/spanning lines make the
    layout ambiguous and are rejected by the caller rather than silently interleaved.
    """
    if not lines:
        return [[]]
    best = None
    for fraction in (0.45, 0.475, 0.5, 0.525, 0.55):
        split = width * fraction
        left = [line for line in lines if line.bbox[2] <= split + 2]
        right = [line for line in lines if line.bbox[0] >= split - 2]
        crossing = [line for line in lines if line not in left and line not in right]
        if len(left) < 6 or len(right) < 6:
            continue
        overlap = min(
            max(item.bbox[3] for item in left), max(item.bbox[3] for item in right)
        ) - max(min(item.bbox[1] for item in left), min(item.bbox[1] for item in right))
        if overlap < 40:
            continue
        gutter = min(item.bbox[0] for item in right) - max(item.bbox[2] for item in left)
        score = (len(crossing), -gutter)
        if gutter >= 5 and (best is None or score < best[0]):
            best = score, left, right, crossing
    if best is None:
        leftish = [item for item in lines if (item.bbox[0] + item.bbox[2]) / 2 < width * 0.45]
        rightish = [item for item in lines if (item.bbox[0] + item.bbox[2]) / 2 > width * 0.55]
        if len(leftish) >= 6 and len(rightish) >= 6:
            overlap = min(
                max(item.bbox[3] for item in leftish), max(item.bbox[3] for item in rightish)
            ) - max(min(item.bbox[1] for item in leftish), min(item.bbox[1] for item in rightish))
            if overlap > 40:
                raise ValueError("ambiguous_column_gutter")
        return [lines]
    _, left, right, crossing = best
    # Titles spanning the page are safe only above both columns.
    top = min(item.bbox[1] for item in left + right)
    if crossing and any(item.bbox[3] > top + 3 for item in crossing):
        raise ValueError("ambiguous_spanning_column_text")
    return ([crossing] if crossing else []) + [left, right]


def merge_rows(lines: list[Line]) -> list[Line]:
    """Reassemble native fragments on one baseline (including ability-score tables)."""
    rows: list[list[Line]] = []
    for line in sorted(lines, key=lambda item: (item.bbox[3], item.bbox[0])):
        if rows and abs(rows[-1][0].bbox[3] - line.bbox[3]) <= 2.5:
            rows[-1].append(line)
        else:
            rows.append([line])
    result = []
    for row in rows:
        row.sort(key=lambda item: item.bbox[0])
        text = " ".join(item.text for item in row)
        bbox = (
            min(item.bbox[0] for item in row),
            min(item.bbox[1] for item in row),
            max(item.bbox[2] for item in row),
            max(item.bbox[3] for item in row),
        )
        result.append(
            Line(text, bbox, max(item.size for item in row), any(item.heading for item in row))
        )
    return result


def _title(line: Line, body_size: float) -> bool:
    alpha = [c for c in line.text if c.isalpha()]
    upper = sum(c.isupper() for c in alpha) / max(1, len(alpha))
    return (
        3 <= len(alpha)
        and len(line.text) < 100
        and re.match(r"^[A-Za-z]", line.text)
        and letters(line.text)
        not in {"STRDEXCONINTWISCHA", "STR", "DEX", "CON", "INT", "WIS", "CHA"}
        and (line.size > body_size * 1.2 or (upper > 0.8 and line.size >= body_size * 0.8))
        and not re.match(r"^(armor class|hit points|speed|challenge)\b", line.text, re.I)
    )


def ocr_stat_header(page, bbox: tuple[float, float, float, float]) -> list[Line]:
    """OCR a small stat-header crop at 600 dpi, retaining high-confidence words only.

    Tesseract is optional for native-only PDFs, required when raster text is missing.
    TSV coordinates let us place supplements without changing any native stat values.
    """
    binary = shutil.which("tesseract")
    if binary is None:
        raise RuntimeError(
            "Missing raster stat heading: install Tesseract with English language data, then extract again."
        )
    pix = page.get_pixmap(clip=pymupdf.Rect(bbox), dpi=600)
    process = subprocess.run(
        [binary, "stdin", "stdout", "-l", "eng", "--psm", "6", "tsv"],
        input=pix.tobytes("png"),
        capture_output=True,
        timeout=45,
        check=True,
    )
    grouped: dict[tuple, list[dict]] = {}
    for word in csv.DictReader(io.StringIO(process.stdout.decode()), delimiter="\t"):
        if word["text"].strip() and float(word["conf"]) >= 75:
            key = tuple(word[k] for k in ("block_num", "par_num", "line_num"))
            grouped.setdefault(key, []).append(word)
    result = []
    scale = 72 / 600
    for words in grouped.values():
        text = " ".join(w["text"] for w in words).strip(" |/.:_—-")
        x0, y0 = min(int(w["left"]) for w in words), min(int(w["top"]) for w in words)
        x1 = max(int(w["left"]) + int(w["width"]) for w in words)
        y1 = max(int(w["top"]) + int(w["height"]) for w in words)
        result.append(
            Line(
                text,
                (
                    bbox[0] + x0 * scale,
                    bbox[1] + y0 * scale,
                    bbox[0] + x1 * scale,
                    bbox[1] + y1 * scale,
                ),
            )
        )
    return result


def render_native_page(
    lines: list[Line], width: float, *, header_ocr=None
) -> tuple[str, list[str]]:
    warnings = []
    body_size = statistics.median(item.size for item in lines) if lines else 10
    ordered = []
    for column in columns(lines, width):
        rows = merge_rows(column)
        for line in rows:
            line.heading = _title(line, body_size)
            if letters(line.text) in STAT_SUBHEADINGS:
                line.heading = True
        first_stat = next((i for i, item in enumerate(rows) if SIZE_TYPE.match(item.text)), None)
        if ordered and first_stat is not None and first_stat > 1:
            # Illustration captions above the next column's separate stat record are
            # source text, but must not extend the previous creature's record.
            prefix = rows[: first_stat - 1]
            if (
                prefix
                and not any(re.match(r"^Armor\s+Class\b", item.text, re.I) for item in prefix)
                and rows[first_stat - 1].heading
                and rows[first_stat - 1].bbox[1] - prefix[-1].bbox[3] > 35
            ):
                rows.insert(0, Line("Illustration text", prefix[0].bbox, body_size * 2, True))
                warnings.append("illustration_caption_separated_from_stat_record")
        for index, ac in enumerate(list(rows)):
            if not re.match(r"^Armor\s+Class\b", ac.text, re.I):
                continue
            previous = [item for item in rows[:index] if ac.bbox[1] - item.bbox[1] <= 65]
            size = next(
                (item for item in reversed(previous) if SIZE_TYPE.match(item.text.strip("*_ "))),
                None,
            )
            title = next(
                (
                    item
                    for item in reversed(previous)
                    if item.heading
                    and letters(item.text) not in STAT_SUBHEADINGS
                    and (size is None or item.bbox[1] < size.bbox[1])
                ),
                None,
            )
            if title is not None and size is not None:
                continue
            if header_ocr is None:
                raise ValueError("missing_stat_heading_or_type")
            left = min(item.bbox[0] for item in column)
            right = max(item.bbox[2] for item in column)
            supplements = header_ocr((left - 2, max(0, ac.bbox[1] - 60), right + 2, ac.bbox[1]))
            raster_size = next((item for item in supplements if SIZE_TYPE.match(item.text)), None)
            raster_title = next(
                (
                    item
                    for item in reversed(supplements)
                    if raster_size is not None
                    and item.bbox[1] < raster_size.bbox[1]
                    and len(re.findall(r"[A-Za-z]", item.text)) >= 3
                    and len(item.text) < 80
                    and _title(item, 0)
                ),
                None,
            )
            if title is None:
                if raster_title is None:
                    raise ValueError("missing_stat_heading")
                raster_title.heading = True
                rows.append(raster_title)
            if size is None:
                if raster_size is None:
                    raise ValueError("missing_stat_type")
                rows.append(raster_size)
            warnings.append("raster_stat_header_ocr_review_required")
        # Some scanned headings (not their surrounding native actions) are absent from
        # the text layer. Inspect only gaps inside a stat column and supplement a known
        # action label. Never synthesize one or import OCR body/number fields.
        if header_ocr is not None:
            sorted_rows = sorted(rows, key=lambda item: item.bbox[1])
            first_ac = next(
                (
                    item.bbox[1]
                    for item in sorted_rows
                    if re.match(r"^Armor\s+Class\b", item.text, re.I)
                ),
                None,
            )
            for before, after in zip(sorted_rows, sorted_rows[1:], strict=False):
                gap = after.bbox[1] - before.bbox[3]
                if first_ac is None or before.bbox[1] < first_ac or not 10 <= gap <= 45:
                    continue
                supplements = header_ocr(
                    (
                        min(item.bbox[0] for item in column) - 2,
                        before.bbox[3],
                        max(item.bbox[2] for item in column) + 2,
                        after.bbox[1],
                    )
                )
                for supplement in supplements:
                    if letters(supplement.text) in STAT_SUBHEADINGS:
                        supplement.heading = True
                        rows.append(supplement)
                        warnings.append("raster_stat_subheading_ocr_review_required")
        ordered.extend(sorted(rows, key=lambda item: (item.bbox[1], item.bbox[0])))
    paragraphs = []
    buf = []
    previous = None
    for line in ordered:
        if line.heading or (
            previous
            and (
                line.bbox[1] - previous.bbox[3] > 5 or line.bbox[0] - previous.bbox[0] > width * 0.3
            )
        ):
            if buf:
                paragraphs.append("\n".join(buf))
                buf = []
        if line.heading:
            title = line.text.strip("*_ ")
            if letters(title) in STAT_SUBHEADINGS:
                title = next(name for name in STAT_SUBHEADINGS if name == letters(title))
                title = re.sub(r"(LEGENDARY|BONUS|LAIR|MYTHIC)(ACTIONS)", r"\1 \2", title)
            elif sum(c.isupper() for c in title) / max(1, sum(c.isalpha() for c in title)) >= 0.6:
                title = " ".join(word.capitalize() for word in title.split())
            paragraphs.append(f"## {title}")
        else:
            buf.append(line.text)
        previous = line
    if buf:
        paragraphs.append("\n".join(buf))
    return "\n\n".join(paragraphs), sorted(set(warnings))
