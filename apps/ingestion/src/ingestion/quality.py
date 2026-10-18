"""Conservative extraction checks, independent of PDF/OCR and of creature identity."""

import re
from collections import Counter

# Stat fields are space-separated ("Armor Class 15", "**Hit Points** 50") at the start of a
# line, so prose such as "regains 20 hit points", class tables such as "Hit Points at 1st
# Level" and colon-style object lines such as "Armor Class: 15" are not counted.
_AC = re.compile(r"^[^\w\n]*Armor\s+Class(?:\*+|_+)?\s+\d", re.I | re.M)
_HP = re.compile(r"^[^\w\n]*Hit\s+Points(?:\*+|_+)?\s+\d", re.I | re.M)
_HEADING = re.compile(r"^#{1,6}\s+(.+)$", re.M)
STAT_SUBHEADINGS = {
    "ACTIONS",
    "REACTIONS",
    "BONUSACTIONS",
    "LEGENDARYACTIONS",
    "LAIRACTIONS",
    "MYTHICACTIONS",
}
SIZE_TYPE = re.compile(
    r"^(tiny|small|medium|large|huge|gargantuan)\b.{0,80}\b(lawful|neutral|chaotic|unaligned|any)\b",
    re.I,
)

_DIAGNOSTIC_MESSAGES = {
    "duplicate_word_layer": "Words were duplicated during extraction. Compare the preview with the PDF and retry with a readable source.",
    "duplicate_text_layer": "A paragraph was extracted more than once. Inspect overlapping text layers before retrying.",
    "interleaved_stat_records": "Statistics from different records were mixed. Inspect column order and creature headings before retrying.",
    "incomplete_stat_fields": "A stat record lost its armor class or hit points. Compare the complete record with the PDF before retrying.",
    "missing_stat_heading": "A stat record has no usable creature heading. Inspect the PDF's headings before retrying.",
    "garbage_stat_heading": "A stat record inherited an unrelated heading. Inspect creature identity and column order before retrying.",
    "missing_stat_heading_or_type": "A creature heading or type could not be read. Inspect the image text and OCR before retrying.",
    "missing_stat_type": "A creature's size or type could not be read. Compare the record with the PDF before retrying.",
    "ambiguous_column_gutter": "The page's columns could not be separated reliably. Inspect column order and upload a clearer PDF.",
    "ambiguous_spanning_column_text": "Text spans the page's columns ambiguously. Inspect reading order and upload a clearer PDF.",
    "partial_native_numbers_lost_in_ocr": "OCR dropped numbers present in the source text. Inspect numeric fields and retry with a readable PDF.",
    "insufficient_ocr_text_on_nonblank_page": "OCR recovered too little text from a content page. Check OCR availability and upload a clearer PDF.",
    "full_page_ocr_review_required": "This page was read with OCR. Compare text, tables and numbers with the PDF.",
    "raster_stat_header_ocr_review_required": "A creature heading was read from an image. Confirm its name, size and type in the PDF.",
    "raster_stat_subheading_ocr_review_required": "A stat subheading was read from an image. Confirm actions and record boundaries in the PDF.",
    "raster_backed_native_layer_completeness_review_required": "This page includes images and text. Check that rules and tables inside images appear in the preview.",
    "source_text_possible_numeric_ocr_ambiguity": "Some source numbers or dice symbols may be ambiguous. Compare them with the PDF before using the rules.",
    "illustration_caption_separated_from_stat_record": "An illustration caption was separated from the stat record. Confirm the creature heading in the preview.",
    "degraded_native_text_layer_replaced_with_ocr": "The PDF's text layer had recurring OCR misspellings, so this page was re-read with OCR. Compare headings, numbers and tables with the PDF.",
    "degraded_native_text_layer_kept": "The PDF's text layer had recurring OCR misspellings, but the OCR re-read was not better, so the text layer was kept. Compare the rules text with the PDF.",
    "degraded_native_text_layer_ocr_unavailable": "The PDF's text layer had recurring OCR misspellings and could not be re-read. Install Tesseract with English data or compare the text with the PDF.",
    "ocr_signature_review_required": "Known OCR misspellings remain in this page's text. Compare the rules text with the PDF before using it.",
}


def diagnostic_message(code: str) -> str:
    """One player-readable adapter; retain original codes in local diagnostics."""
    if code.startswith("removed_identical_overlapping_spans:"):
        return "Repeated overlapping text spans were removed. Confirm the preview matches the PDF."
    # Layout codes can carry a bounding box after the code, e.g. "... band=(...)".
    return _DIAGNOSTIC_MESSAGES.get(code.split(" ", 1)[0], code.replace("_", " "))


def has_stat_record(text: str) -> bool:
    """A page with AC/HP fields or a size/type line is never eligible for exclusion."""
    return bool(
        _AC.search(text)
        or _HP.search(text)
        or any(SIZE_TYPE.match(line.strip("*_ ")) for line in text.splitlines())
    )


def has_section_heading(text: str) -> bool:
    """Level-1 or level-2 headings may be rules sections; excluded pages are flagged for review."""
    return bool(re.search(r"^#{1,2}\s", text, re.M))


def letters(text: str) -> str:
    return re.sub(r"[^A-Z]", "", text.upper())


def check_text(text: str) -> dict:
    """Detect heavy duplicate layers and mixed stat records without changing any numbers.

    Adjacent word repetitions are counted after case folding and punctuation removal.
    A few legitimate repetitions are allowed; systematic duplicated OCR words are not.
    Each named section may contain at most one AC/HP pair. This catches both interleaved
    fields and two records accidentally concatenated under a generic Actions heading.
    """
    words = re.findall(r"\b[\w]+\b", text.casefold())
    repeated = sum(a == b for a, b in zip(words, words[1:], strict=False))
    errors = []
    if repeated >= 8 and repeated / max(1, len(words)) > 0.04:
        errors.append("duplicate_word_layer")
    sections = []
    start = 0
    for heading in _HEADING.finditer(text):
        title = heading.group(1).strip("*_ ")
        if letters(title) in STAT_SUBHEADINGS:
            continue
        sections.append(text[start : heading.start()])
        start = heading.start()
    sections.append(text[start:])
    for section in sections:
        paras = [re.sub(r"\s+", " ", p).strip() for p in section.split("\n\n")]
        if any(count > 1 and len(p) > 100 for p, count in Counter(paras).items()):
            errors.append("duplicate_text_layer")
        ac, hp = len(_AC.findall(section)), len(_HP.findall(section))
        if ac > 1 or hp > 1:
            errors.append("interleaved_stat_records")
        stat_type = any(SIZE_TYPE.match(line.strip("*_ ")) for line in section.splitlines())
        if (ac or stat_type) and ac != hp:
            errors.append("incomplete_stat_fields")
        if ac and hp:
            headings = [h.group(1).strip("*_ ") for h in _HEADING.finditer(section)]
            useful = [title for title in headings if letters(title) not in STAT_SUBHEADINGS]
            if not useful:
                errors.append("missing_stat_heading")
            elif SIZE_TYPE.match(useful[0]) or re.match(
                r"^(armor class|hit points|speed|monster manual)\b",
                useful[0],
                re.I,
            ):
                errors.append("garbage_stat_heading")
    return {
        "characters": len(text),
        "words": len(words),
        "adjacent_repeated_words": repeated,
        "armor_class_fields": len(_AC.findall(text)),
        "hit_points_fields": len(_HP.findall(text)),
        "errors": sorted(set(errors)),
    }


def require_separate_stat_chunks(chunks: list[dict]) -> None:
    for chunk in chunks:
        if len(_AC.findall(chunk["text"])) > 1 or len(_HP.findall(chunk["text"])) > 1:
            raise ValueError(
                f"Mixed stat records in chunk {chunk['index']} on page {chunk['page']}; "
                "inspect headings and column order before indexing."
            )
        if chunk.get("stat_block") and bool(_AC.search(chunk["text"])) != bool(
            _HP.search(chunk["text"])
        ):
            raise ValueError(
                f"Incomplete stat fields in chunk {chunk['index']} on page {chunk['page']}."
            )
