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

    Require substantial vertical overlap and a clear gutter. Spanning lines below the column
    tops are segmented into full-width zones and two-column regions; a zone that cannot be read
    unambiguously fails closed with its bounding box rather than silently interleaving.
    """
    if not lines:
        return [[]]
    candidates = []
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
        if gutter >= 5:
            candidates.append((score, left, right, crossing))
    if not candidates:
        leftish = [item for item in lines if (item.bbox[0] + item.bbox[2]) / 2 < width * 0.45]
        rightish = [item for item in lines if (item.bbox[0] + item.bbox[2]) / 2 > width * 0.55]
        if len(leftish) >= 6 and len(rightish) >= 6:
            overlap = min(
                max(item.bbox[3] for item in leftish), max(item.bbox[3] for item in rightish)
            ) - max(min(item.bbox[1] for item in leftish), min(item.bbox[1] for item in rightish))
            if overlap > 40:
                raise ValueError("ambiguous_column_gutter")
        return [lines]
    candidates.sort(key=lambda candidate: candidate[0])
    _, left, right, crossing = candidates[0]
    # Titles spanning the page are safe only above both columns.
    top = min(item.bbox[1] for item in left + right)
    if not any(item.bbox[3] > top + 3 for item in crossing):
        return ([crossing] if crossing else []) + [left, right]
    # A gutter that cuts through a table or prose can still leave a clean reading under another
    # candidate split; take the first one that reads without ambiguity, else keep the first error.
    errors = []
    for _, left, right, crossing in candidates:
        try:
            return _segment(lines, left, right, crossing)
        except ValueError as exc:
            errors.append(exc)
    raise errors[0]


def _segment(lines: list[Line], left: list[Line], right: list[Line], crossing: list[Line]):
    """Read spanning lines below the column tops as full-width zones, not as an interleave.

    Vertical runs of lines (gaps of at most 5 pt) holding a spanning line form a zone. A zone is
    read top to bottom as one block when it is a single row, spanning-only, or a table (see
    _full_width_zone). Other runs are two-column regions read left then right. A zone that fits
    none of these fails closed with its bounding box.
    Symbol-only spanning glyphs, such as a decorative bullet, carry no text and are omitted.
    Mostly-symbol spanning strips, such as a page-header ornament read as punctuation, are too.
    """
    spanning = {
        id(item)
        for item in crossing
        if any(c.isalnum() for c in item.text) and not _ornament(item.text)
    }
    symbols = {id(item) for item in crossing} - spanning
    left_ids = {id(item) for item in left}
    runs: list[list[Line]] = []
    bottom = 0.0
    for item in sorted((i for i in lines if id(i) not in symbols), key=lambda i: i.bbox[1]):
        if not runs or item.bbox[1] - bottom > 5:
            runs.append([])
        runs[-1].append(item)
        bottom = max(bottom, item.bbox[3])

    def region_columns(region: list[Line]) -> list[list[Line]]:
        return [
            [item for item in region if id(item) in left_ids],
            [item for item in region if id(item) not in left_ids],
        ]

    ordered: list[list[Line]] = []
    region: list[Line] = []
    for run in runs:
        if not any(id(item) in spanning for item in run):
            region.extend(run)
            continue
        ordered += region_columns(region)
        region = []
        if not _full_width_zone(run, spanning):
            box = (
                min(i.bbox[0] for i in run),
                min(i.bbox[1] for i in run),
                max(i.bbox[2] for i in run),
                max(i.bbox[3] for i in run),
            )
            raise ValueError(
                "ambiguous_spanning_column_text band=({:.1f},{:.1f},{:.1f},{:.1f})".format(*box)
            )
        ordered.append(run)
    ordered += region_columns(region)
    return [column for column in ordered if column]


def _ornament(text: str) -> bool:
    """True for a strip that is mostly punctuation, e.g. a decorative page-header rule."""
    chars = [c for c in text if not c.isspace()]
    return sum(c.isalnum() for c in chars) * 3 < len(chars)


def _full_width_zone(run: list[Line], spanning: set[int]) -> bool:
    """Accept a zone only when reading it row by row cannot mix two column streams.

    A zone is safe when every line spans (a title, label or full-width intro), sits on one
    baseline, or is a table: at least three rows carry a spanning cell and three quarters of
    the rows hold three or more cells. Rows with blank cells, such as an empty "Previous Rulers"
    entry, may leave a table's spanning rows fewer than a third of its rows, so the spanning
    rows are then also judged on their own. Prose columns cut by a gutter that is too narrow
    give mostly two-cell rows, so they do not qualify.
    """
    rows: list[list[Line]] = []
    for item in sorted(run, key=lambda i: i.bbox[3]):
        if rows and abs(rows[-1][0].bbox[3] - item.bbox[3]) <= 2.5:
            rows[-1].append(item)
        else:
            rows.append([item])
    if len(rows) == 1 or all(id(item) in spanning for item in run):
        return True
    span_rows = [row for row in rows if any(id(item) in spanning for item in row)]
    cells = sum(len(row) >= 3 for row in span_rows)
    wide = sum(len(row) >= 3 for row in rows)
    return len(span_rows) >= 3 and (
        (3 * len(span_rows) >= len(rows) and 4 * cells >= 3 * len(span_rows))
        or 4 * wide >= 3 * len(rows)
    )


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


# A stat field has a number after its label; prose headings such as "ARMOR CLASS" do not.
_STAT_AC = re.compile(r"^[^\w\n]*Armor\s+Class(?:\*+|_+)?\s+\d", re.I)


def _raster_header(
    supplements: list[Line], size: Line | None, body_size: float, top: float
) -> tuple[Line | None, Line | None]:
    """Pick a raster creature size/type line and the name above it from OCR supplements."""
    raster_size = next((item for item in supplements if SIZE_TYPE.match(item.text)), None)
    # A native size line may also be re-read by OCR; the name's centre must sit above it.
    anchor = size if size is not None else raster_size
    boundary = anchor.bbox[1] if anchor is not None else top
    raster_title = next(
        (
            item
            for item in reversed(supplements)
            if (item.bbox[1] + item.bbox[3]) / 2 < boundary
            and len(re.findall(r"[A-Za-z]", item.text)) >= 3
            and len(item.text) < 80
            and letters(item.text) not in STAT_SUBHEADINGS
            # OCR case is unreliable ("Twig Blight" reads "Twic BLIGHT"): test the
            # shape on an uppercased copy and keep the original spelling.
            and _title(Line(item.text.upper(), item.bbox, body_size), body_size)
        ),
        None,
    )
    return raster_size, raster_title


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
        # A creature name sits directly above its size/type line. Only a tight, sentence-free
        # line qualifies, so sidebar captions and prose far above a stat record never do.
        for name, size_line in zip(rows, rows[1:], strict=False):
            if (
                SIZE_TYPE.match(size_line.text)
                and abs(size_line.bbox[1] - name.bbox[3]) <= 4
                and 3 <= len(letters(name.text))
                and len(name.text) < 60
                and not re.search(r"[.,;:!?]$", name.text)
                and not re.match(r"^(armor class|hit points|speed)\b", name.text, re.I)
            ):
                name.heading = True
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
            # Only a stat field counts: an uppercase "ARMOR CLASS" section heading in prose is not one.
            if not _STAT_AC.match(ac.text):
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
            top = ac.bbox[1]
            # OCR is sensitive to crop edges: a name can be dropped by one crop and read by the
            # next. Later crops are tried only when the earlier ones yield no usable header; the
            # last two sit just above a native size line when one is present.
            near = size.bbox[1] if size is not None else top
            for box in (
                (left - 2, max(0, top - 60), right + 2, top),
                (left - 12, max(0, top - 60), right + 12, top),
                (left - 12, max(0, top - 45), right + 12, top),
                (left - 12, max(0, top - 72), right + 12, top),
                (left - 12, max(0, near - 40), right + 12, near),
                (left - 12, max(0, near - 30), right + 12, near),
            ):
                supplements = header_ocr(box)
                raster_size, raster_title = _raster_header(supplements, size, body_size, top)
                if (title is not None or raster_title is not None) and (
                    size is not None or raster_size is not None
                ):
                    break
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
                title = re.sub(r"(LEGENDARY|BONUS|LAIR|MYTHIC)(ACTIONS)", r"\1 \2", letters(title))
            elif sum(c.isupper() for c in title) / max(1, sum(c.isalpha() for c in title)) >= 0.6:
                title = " ".join(word.capitalize() for word in title.split())
            paragraphs.append(f"## {title}")
        else:
            buf.append(line.text)
        previous = line
    if buf:
        paragraphs.append("\n".join(buf))
    return "\n\n".join(paragraphs), sorted(set(warnings))
