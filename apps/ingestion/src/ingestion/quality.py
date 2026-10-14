"""Conservative extraction checks, independent of PDF/OCR and of creature identity."""

import re
from collections import Counter

_AC = re.compile(r"\bArmor\s+Class\b", re.I)
_HP = re.compile(r"\bHit\s+Points\b", re.I)
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


def require_clean_pages(pages: list[tuple[int, str]]) -> None:
    failures = {page: check_text(text)["errors"] for page, text in pages}
    failures = {page: errors for page, errors in failures.items() if errors}
    if failures:
        raise ValueError(
            f"Extraction quality checks failed: {failures}. Inspect the extraction preview."
        )


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
