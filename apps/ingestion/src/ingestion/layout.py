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


ILLUSTRATION_SIZE_RATIO = 0.65


@dataclass
class Line:
    text: str
    bbox: tuple[float, float, float, float]
    size: float = 0
    heading: bool = False
    # Lowest word confidence on an OCR line; native text is always 100.
    confidence: float = 100.0
    # Small text printed inside an illustration, e.g. an item caption; see native_lines.
    illustration: bool = False


def native_lines(data: dict, height: float) -> tuple[list[Line], int]:
    """Keep source strings and geometry; remove only identical overlapping spans."""
    out = []
    seen: dict[str, list[tuple]] = {}
    duplicates = 0
    figures = [tuple(block["bbox"]) for block in data["blocks"] if block.get("type") == 1]
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
    if out and figures:
        body_size = statistics.median(item.size for item in out)
        for item in out:
            # A caption is far below body size and sits inside an image; page-sized background
            # images hold body text too, but at body size, so size alone separates the two.
            item.illustration = item.size <= body_size * ILLUSTRATION_SIZE_RATIO and any(
                rect[0] - 2 <= item.bbox[0]
                and rect[1] - 2 <= item.bbox[1]
                and item.bbox[2] <= rect[2] + 2
                and item.bbox[3] <= rect[3] + 2
                for rect in figures
            )
    return out, duplicates


def columns(lines: list[Line], width: float) -> list[list[Line]]:
    """Read a page's columns, retrying with drop-cap and illustration handling only on failure.

    A page the plain reading accepts is never altered. A page it rejects is retried with, in
    turn, a decorative drop cap merged into its word, the illustration captions set apart, and
    both; the first reading that succeeds is used, otherwise the plain error is raised.
    """
    try:
        return _columns(lines, width)
    except ValueError as plain:
        merged = merge_drop_caps(lines)
        captions = [item for item in lines if item.illustration]
        for body, caption_lines in (
            (merged, []),
            ([item for item in lines if not item.illustration], captions),
            ([item for item in merged if not item.illustration], captions),
        ):
            if body is lines or len(body) == len(lines) and not caption_lines:
                continue
            try:
                result = _columns(body, width)
            except ValueError:
                continue
            blocks = _caption_blocks(caption_lines)
            if blocks and result and min(i.bbox[1] for b in blocks for i in b) > min(
                i.bbox[1] for b in result for i in b
            ):
                return result + blocks
            return blocks + result
        raise plain


def _caption_blocks(captions: list[Line]) -> list[list[Line]]:
    """Group illustration captions into blocks of neighbouring lines, one block per caption."""
    blocks: list[list[Line]] = []
    for item in sorted(captions, key=lambda i: (i.bbox[1], i.bbox[0])):
        for block in blocks:
            if any(
                item.bbox[1] - other.bbox[3] <= 6
                and item.bbox[1] >= other.bbox[1]
                and item.bbox[0] < other.bbox[2]
                and other.bbox[0] < item.bbox[2]
                for other in block
            ):
                block.append(item)
                break
        else:
            blocks.append([item])
    return blocks


def merge_drop_caps(lines: list[Line]) -> list[Line]:
    """Join a tall decorative initial to the first line it introduces ('T' + 'he' -> 'The').

    The initial is a short glyph at least 2.5 times the body size. It must stand left of at least
    two lines that begin to its right inside its height; the first of those continues the
    word. Only its letters and digits are kept, so a flourish read as punctuation is dropped,
    and nothing is guessed: a mis-read initial stays as the source gave it.
    """
    if not lines:
        return lines
    body_size = statistics.median(item.size for item in lines)
    result = list(lines)
    for glyph in lines:
        if glyph.size < body_size * 2.5 or len(glyph.text) > 4:
            continue
        beside = sorted(
            (
                item
                for item in lines
                if item is not glyph
                and item.size < glyph.size / 2
                and item.bbox[1] >= glyph.bbox[1] - 1
                and item.bbox[3] <= glyph.bbox[3] + 1
                and glyph.bbox[0] - 3 <= item.bbox[0] <= glyph.bbox[2] + 40
            ),
            key=lambda item: item.bbox[1],
        )
        if len(beside) < 2 or beside[1].bbox[0] < glyph.bbox[2] - 25:
            continue
        first = beside[0]
        letters_only = "".join(c for c in glyph.text if c.isalnum())
        merged = Line(
            letters_only + first.text,
            (min(glyph.bbox[0], first.bbox[0]), *first.bbox[1:]),
            first.size,
        )
        result = [merged if item is first else item for item in result if item is not glyph]
    return result


def _columns(lines: list[Line], width: float) -> list[list[Line]]:
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
        if _anchored_table(lines):
            return [lines]
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
    if not any(item.bbox[3] > top + 3 for item in crossing) and not _anchored_table(lines):
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
    """Read the page by zones; a zone the plain reading rejects may still be a table band.

    The band is found only after the plain reading failed, so no page it accepts changes.
    """
    try:
        return _segment_zones(lines, left, right, crossing)
    except ValueError:
        band = _table_band(lines)
        if band is None:
            raise
        ids = {id(item) for item in band}
        top = min(item.bbox[1] for item in band)
        bottom = max(item.bbox[3] for item in band)
        parts = []
        for part in (
            [i for i in lines if id(i) not in ids and i.bbox[3] <= top + 1],
            band,
            [i for i in lines if id(i) not in ids and i.bbox[3] > top + 1],
        ):
            if part is band:
                parts.append([i for i in band if any(c.isalnum() for c in i.text)])
                continue
            keep = {id(i) for i in part}
            parts += _segment_zones(
                part,
                [i for i in left if id(i) in keep],
                [i for i in right if id(i) in keep],
                [i for i in crossing if id(i) in keep],
            )
        return [column for column in parts if column]


def _table_rows(lines: list[Line]) -> list[list[Line]]:
    rows: list[list[Line]] = []
    for item in sorted(lines, key=lambda i: i.bbox[3]):
        if rows and abs(rows[-1][0].bbox[3] - item.bbox[3]) <= 2.5:
            rows[-1].append(item)
        else:
            rows.append([item])
    return rows


def _table_band(lines: list[Line]) -> list[Line] | None:
    """The largest vertical band of whole rows that is an anchored table (see _anchored_table).

    Bands start and end at a visible gap between rows, so prose is never cut mid-paragraph.
    Blank cells leave rows short but the anchors of the other rows still identify the table.
    """
    rows = _table_rows(lines)

    def gap(index: int) -> float:
        return min(i.bbox[1] for i in rows[index]) - max(i.bbox[3] for i in rows[index - 1])

    starts = [0] + [i for i in range(1, len(rows)) if gap(i) >= 3]
    ends = [i for i in range(1, len(rows)) if gap(i) >= 3] + [len(rows)]
    best: list[Line] | None = None
    for start in starts:
        for end in reversed(ends):
            if end - start < 3 or (best and sum(map(len, rows[start:end])) <= len(best)):
                continue
            band = [item for row in rows[start:end] for item in row]
            if _anchored_table(band):
                best = band
                break
    return best


def _segment_zones(lines: list[Line], left: list[Line], right: list[Line], crossing: list[Line]):
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
    for run in (part for run in runs for part in _split_table_tail(run, spanning)):
        if not any(id(item) in spanning for item in run) and not _anchored_table(run):
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
        ordered.append([item for item in run if id(item) in spanning or any(c.isalnum() for c in item.text)])
    ordered += region_columns(region)
    return [column for column in ordered if column]


def _ornament(text: str) -> bool:
    """True for a strip that is mostly punctuation, e.g. a decorative page-header rule."""
    chars = [c for c in text if not c.isspace()]
    return sum(c.isalnum() for c in chars) * 3 < len(chars)


def _anchored_table(items: list[Line]) -> bool:
    """True for a table whose cells share column starts, even when some cells are blank.

    Column starts within 20 pt chain into one anchor, so paragraph indents and two-column prose
    give only two anchors. A table needs three anchors, each used by three or more rows, and
    most cells must sit on those anchors and three rows must place two or more cells on them. Blank cells, such as an em dash
    missing from the text layer, leave no text behind, so nothing is invented.
    """
    rows: list[list[Line]] = []
    for item in sorted(items, key=lambda i: i.bbox[3]):
        if rows and abs(rows[-1][0].bbox[3] - item.bbox[3]) <= 2.5:
            rows[-1].append(item)
        else:
            rows.append([item])
    starts = sorted((item.bbox[0], index) for index, row in enumerate(rows) for item in row)
    clusters: list[list[tuple[float, int]]] = []
    for start in starts:
        if clusters and start[0] - clusters[-1][-1][0] <= 20:
            clusters[-1].append(start)
        else:
            clusters.append([start])
    anchors = [
        (cluster[0][0], cluster[-1][0])
        for cluster in clusters
        if len({index for _, index in cluster}) >= 3
    ]
    if len(anchors) < 3 or len(rows) < 3:
        return False
    on_anchor = [any(lo <= item.bbox[0] <= hi for lo, hi in anchors) for item in items]
    cells_per_row = [
        sum(any(lo <= item.bbox[0] <= hi for lo, hi in anchors) for item in row) for row in rows
    ]
    # Wrapped continuation rows hold one cell, so judge the share of cells, not of rows.
    if not (sum(count >= 2 for count in cells_per_row) >= 3 and 4 * sum(on_anchor) >= 3 * len(items)):
        return False
    # Table cells are short. Prose lines run the width of a column, so two columns of prose beside
    # a table never qualify, however their line starts line up. A wide notes column is allowed
    # when at least two other columns hold short cells.
    span = max(item.bbox[2] for item in items) - min(item.bbox[0] for item in items)
    short = 0
    for lo, hi in anchors:
        cells = [item for item in items if lo <= item.bbox[0] <= hi]
        short += 2 * sum(item.bbox[2] - item.bbox[0] <= span * 0.3 for item in cells) > len(cells)
    return short >= 2


def _split_table_tail(run: list[Line], spanning: set[int]) -> list[list[Line]]:
    """Split a run that ends in a table block from the prose above it, so each part reads alone.

    Rows with three or more cells form the table; a one-cell row (a table heading) may sit inside
    it. Two-cell rows are two-column prose and stay above the split.
    """
    if not any(id(item) in spanning for item in run):
        return [run]
    rows: list[list[Line]] = []
    for item in sorted(run, key=lambda i: i.bbox[3]):
        if rows and abs(rows[-1][0].bbox[3] - item.bbox[3]) <= 2.5:
            rows[-1].append(item)
        else:
            rows.append([item])
    start = len(rows)
    while start > 0 and (len(rows[start - 1]) >= 3 or len(rows[start - 1]) == 1):
        start -= 1
    # Leading one-cell rows are the table heading; the table proper starts at its first 3-cell row.
    while start < len(rows) and len(rows[start]) < 3:
        start += 1
    tail = [item for row in rows[start:] for item in row]
    head = [item for row in rows[:start] for item in row]
    if head and sum(len(row) >= 3 for row in rows[start:]) >= 3 and _anchored_table(tail):
        return [head, tail]
    return [run]


def _full_width_zone(run: list[Line], spanning: set[int]) -> bool:
    """Accept a zone only when reading it row by row cannot mix two column streams.

    A zone is safe when every line spans (a title, label or full-width intro), sits on one
    baseline, or is a table: at least three rows carry a spanning cell and three quarters of
    the rows hold three or more cells. Rows with blank cells, such as an empty "Previous Rulers"
    entry, may leave a table's spanning rows fewer than a third of its rows, so the spanning
    rows are then also judged on their own. Prose columns cut by a gutter that is too narrow
    give mostly two-cell rows, so they do not qualify.
    """
    if _anchored_table(run):
        return True
    rows: list[list[Line]] = []
    for item in sorted(run, key=lambda i: i.bbox[3]):
        if rows and abs(rows[-1][0].bbox[3] - item.bbox[3]) <= 2.5:
            rows[-1].append(item)
        else:
            rows.append([item])
    if len(rows) == 1 or all(id(item) in spanning for item in run):
        return True
    # A paragraph band with a decorative corner glyph (no letters or digits) is still one band.
    if all(id(item) in spanning or not any(c.isalnum() for c in item.text) for item in run):
        return True
    span_rows = [row for row in rows if any(id(item) in spanning for item in row)]
    cells = sum(len(row) >= 3 for row in span_rows)
    wide = sum(len(row) >= 3 for row in rows)
    # A table whose gutter-crossing cells are few (e.g. one wrapped feature column) still reads
    # row by row when most of its rows hold three or more cells.
    return (len(rows) >= 3 and 4 * wide >= 3 * len(rows)) or (
        len(span_rows) >= 3 and 3 * len(span_rows) >= len(rows) and 4 * cells >= 3 * len(span_rows)
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
                confidence=min(float(w["conf"]) for w in words),
            )
        )
    return result


def _recovered_entry(line: Line, kind: str, crop: int) -> dict:
    return {
        "text": line.text,
        "kind": kind,
        "confidence": round(line.confidence),
        "crop_index": crop,
    }


def render_native_page(
    lines: list[Line], width: float, *, header_ocr=None, recovered: list[dict] | None = None
) -> tuple[str, list[str]]:
    """Render a native page; `recovered` collects every heading or type line read from a raster.

    Each record carries the OCR text as read, its kind, its lowest word confidence and the crop
    that produced it, so an operator can check it against the page image.
    """
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
            for crop, box in enumerate(
                (
                    (left - 2, max(0, top - 60), right + 2, top),
                    (left - 12, max(0, top - 60), right + 12, top),
                    (left - 12, max(0, top - 45), right + 12, top),
                    (left - 12, max(0, top - 72), right + 12, top),
                    (left - 12, max(0, near - 40), right + 12, near),
                    (left - 12, max(0, near - 30), right + 12, near),
                )
            ):
                supplements = header_ocr(box)
                raster_size, raster_title = _raster_header(supplements, size, body_size, top)
                chosen = crop
                if (title is not None or raster_title is not None) and (
                    size is not None or raster_size is not None
                ):
                    break
            if title is None:
                if raster_title is None:
                    raise ValueError("missing_stat_heading")
                raster_title.heading = True
                rows.append(raster_title)
                if recovered is not None:
                    recovered.append(_recovered_entry(raster_title, "name", chosen))
            if size is None:
                if raster_size is None:
                    raise ValueError("missing_stat_type")
                rows.append(raster_size)
                if recovered is not None:
                    recovered.append(_recovered_entry(raster_size, "size_type", chosen))
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
                        if recovered is not None:
                            recovered.append(_recovered_entry(supplement, "subheading", 0))
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
