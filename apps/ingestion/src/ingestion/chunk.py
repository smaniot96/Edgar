"""Heading-aware chunker for extracted PDF markdown (pymupdf4llm) or generated markdown text.

Strategy:
  1. Parse every page into a stream of markdown headings (`#`..`######`) and paragraphs
     (blank-line separated; tables stay together because they have no blank lines inside).
  2. Headings open a new *section* and maintain a heading stack. "Chapter / Part / Appendix"
     headings are treated as top level (they reset the stack) so they become the `chapter`.
     Each chunk's text is prefixed with its heading path ("Chapter 2 > The Temple > Area 3"),
     which grounds the embedding and tells the LLM where the excerpt comes from.
  3. Stat blocks are kept together: inside a section that looks like a stat block ("Armor
     Class" + "Hit Points"/"Challenge", or a "Large fiend (devil), lawful evil" line), the
     sub-headings ACTIONS / REACTIONS / LEGENDARY ACTIONS / ... do not open new sections, and
     a complete stat block stays together by default; an explicit `stat_block_max` caps it. Lore text that
     precedes the stat block in the same section is chunked separately.
  4. Sections longer than the cap are packed paragraph by paragraph; consecutive chunks of the
     same section share `overlap` characters of carry-over. A single paragraph longer than
     the cap falls back to a whitespace-aware sliding window.
  5. Sections may span pages: `page` is the page the chunk starts on, `page_end` where it ends.

Each chunk dict carries `{"text", "page", "page_end", "index", "source", "section", "chapter",
"heading_path", "stat_block"}`. `index` is a global counter included in the immutable index
identity and idempotent Qdrant point ids. `section`/`chapter`/`heading_path` are None for text
before the first heading.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ingestion.quality import SIZE_TYPE, STAT_SUBHEADINGS

ChunkDict = dict[str, Any]

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_CHAPTER_RE = re.compile(r"^(chapter|part|appendix|book)\b", re.IGNORECASE)
# Stat-block sub-headings, compared letters-only so OCR noise like "AC T IONS" still matches.
# pymupdf sometimes promotes a stat line ("Armor Class 7") to a heading.
_STAT_FIELD_HEADING_RE = re.compile(
    r"^(armor class|hit points|speed|challenge|saving throws|skills|senses|languages|str\b)",
    re.IGNORECASE,
)
_AC_RE = re.compile(r"Armor Class\W{0,6}\d")
_HP_RE = re.compile(r"Hit Points\W{0,6}\d")
# pymupdf4llm image placeholders carry no text worth embedding.
_NOISE_LINE_RE = re.compile(
    r"(==> picture \[.*\] intentionally omitted <==|-{3,} (start|end) of picture text -{3,})", re.I
)
_PATH_SEP = " > "


@dataclass
class _Heading:
    level: int
    title: str
    is_chapter: bool
    page: int


@dataclass
class _Section:
    path: list[_Heading]
    paras: list[tuple[str, int]] = field(default_factory=list)  # (text, page)

    def looks_like_stat_block(self) -> bool:
        return _stat_block_start(self.paras) is not None


def chunk_pages(
    pages: list[tuple[int, str]],
    source: str,
    chunk_size: int = 1000,
    overlap: int = 100,
    stat_block_max: int | None = None,
) -> list[ChunkDict]:
    """Split `(page, markdown)` pages into heading-aware chunks (see module docstring).

    Complete stat records are indivisible by default. Set `stat_block_max` explicitly
    only when the caller intentionally permits splitting a record.
    """
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    overlap = max(0, min(overlap, chunk_size // 2))
    stat_cap = stat_block_max if stat_block_max is not None else 4 * chunk_size

    sections = _build_sections(pages)

    chunks: list[ChunkDict] = []
    for section in sections:
        if not section.paras and not section.path:
            continue
        titles = [h.title for h in section.path]
        heading_path = _PATH_SEP.join(titles) if titles else None
        chapter = next((h.title for h in section.path if h.is_chapter), None)
        name = titles[-1] if titles else None
        if not section.paras:
            # Heading-only title/section pages contain real source text.
            section.paras = [(name, section.path[-1].page)]

        stat_idx = _stat_block_start(section.paras)
        groups: list[tuple[list[tuple[str, int]], bool]] = []
        if stat_idx is None:
            groups.append((section.paras, False))
        else:
            if stat_idx > 0:
                groups.append((section.paras[:stat_idx], False))
            groups.append((section.paras[stat_idx:], True))

        for paras, is_stat in groups:
            # A complete default stat record is indivisible. Explicit callers may still
            # request a cap; ordinary PDF ingestion must not cut off actions/continuations.
            cap = stat_cap if is_stat else chunk_size
            if is_stat and stat_block_max is None:
                cap = max(cap, sum(len(p) + 2 for p, _ in paras) + len(heading_path or "") + 1)
            for body, page, page_end, source_pages in _pack(
                paras, heading_path, cap, overlap, [(h.title, h.page) for h in section.path]
            ):
                chunks.append(
                    {
                        "text": body,
                        "page": page,
                        "page_end": page_end,
                        "source_pages": source_pages,
                        "index": len(chunks),
                        "source": source,
                        "section": name,
                        "chapter": chapter,
                        "heading_path": heading_path,
                        "stat_block": is_stat,
                    }
                )
    return chunks


# --- parsing ---------------------------------------------------------------------------------


def _build_sections(pages: list[tuple[int, str]]) -> list[_Section]:
    stack: list[_Heading] = []
    current = _Section(path=[])
    sections: list[_Section] = [current]

    for page_num, text in pages:
        if not text or not text.strip():
            continue
        for kind, value in _blocks(text):
            if kind == "para":
                current.paras.append((value, page_num))
                continue

            level, title = value
            letters = re.sub(r"[^A-Z]", "", title.upper())
            if current.looks_like_stat_block() and (
                letters in STAT_SUBHEADINGS or _STAT_FIELD_HEADING_RE.match(title)
            ):
                # Part of the stat block: keep it in the same section, as plain text.
                current.paras.append(
                    (title.upper() if letters in STAT_SUBHEADINGS else title, page_num)
                )
                continue

            is_chapter = bool(_CHAPTER_RE.match(title))
            level = 0 if is_chapter else level
            while stack and stack[-1].level >= level:
                stack.pop()
            stack.append(_Heading(level=level, title=title, is_chapter=is_chapter, page=page_num))
            current = _Section(path=list(stack))
            sections.append(current)
    return sections


def _blocks(text: str):
    """Yield ("heading", (level, title)) and ("para", text) in document order."""
    buf: list[str] = []

    def flush():
        if buf:
            para = "\n".join(buf).strip()
            buf.clear()
            if para:
                return para
        return None

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if _NOISE_LINE_RE.search(line):
            line = ""
        if not line.strip():
            para = flush()
            if para:
                yield "para", para
            continue
        m = _HEADING_RE.match(line.strip())
        if m:
            title = _clean_heading(m.group(2))
            para = flush()
            if para:
                yield "para", para
            if _is_real_heading(title):
                yield "heading", (len(m.group(1)), title)
            elif title:
                yield "para", title
            continue
        buf.append(line)
    para = flush()
    if para:
        yield "para", para


def _clean_heading(raw: str) -> str:
    title = re.sub(r"[*_`]+", "", raw)
    title = re.sub(r"\s+", " ", title).strip(" :.-")
    letters = [c for c in title if c.isalpha()]
    if letters and sum(c.isupper() for c in letters) / len(letters) >= 0.8:
        title = " ".join(w.capitalize() for w in title.split())
    return title


def _is_real_heading(title: str) -> bool:
    return len(re.findall(r"[A-Za-z]", title)) >= 2 and len(title) <= 150


def _stat_block_start(paras: list[tuple[str, int]]) -> int | None:
    """Index of the paragraph where a stat block begins, or None if there is none."""
    for i, (p, _) in enumerate(paras):
        if SIZE_TYPE.match(p.lstrip("_* \t")):
            # Make sure it really is a stat block: stats must follow within a few paragraphs.
            window = " ".join(t for t, _ in paras[i : i + 4])
            if _AC_RE.search(window) or _HP_RE.search(window):
                return i
        if _AC_RE.search(p):
            window = " ".join(t for t, _ in paras[i : i + 4])
            if _HP_RE.search(window):
                # Pull in a short name line right before the stats (e.g. "**Goblin**").
                if i > 0 and len(paras[i - 1][0]) <= 60 and "\n" not in paras[i - 1][0]:
                    return i - 1
                return i
    return None


# --- packing ---------------------------------------------------------------------------------


def _pack(
    paras: list[tuple[str, int]],
    heading_path: str | None,
    cap: int,
    overlap: int,
    heading_origins: list[tuple[str, int]],
) -> list[tuple[str, int, int, list[int]]]:
    """Greedy paragraph packing under `cap` chars (heading prefix included)."""
    prefix = ""
    if heading_path:
        prefix = (
            heading_path
            if len(heading_path) <= cap // 3
            else heading_path[: max(0, cap // 3 - 1)] + "…"
        )
    budget = cap - (len(prefix) + 1 if prefix else 0)
    if budget < max(20, cap // 3):
        prefix, budget = "", cap
    overlap = min(overlap, budget // 3)
    prefix_pages = set()
    prefix_position = 0
    visible_prefix = len(prefix.rstrip("…"))
    for title, page in heading_origins:
        if prefix_position < visible_prefix:
            prefix_pages.add(page)
        prefix_position += len(title) + len(_PATH_SEP)

    def emit(body: str) -> str:
        return f"{prefix}\n{body}" if prefix else body

    out: list[tuple[str, int, int, list[int]]] = []
    cur: list[str] = []
    cur_len = 0
    cur_start: int | None = None
    cur_end: int | None = None
    carry = ""  # overlap tail from the previous chunk of this section
    cur_spans: list[tuple[int, int, int]] = []  # exact body character ranges -> source page
    carry_spans: list[tuple[int, int, int]] = []

    def flush() -> None:
        nonlocal cur, cur_len, cur_start, cur_end, carry, cur_spans, carry_spans
        if not cur:
            return
        body = "\n\n".join(cur)
        out.append(
            (emit(body), cur_start, cur_end, sorted(prefix_pages | {p for _, _, p in cur_spans}))
        )
        carry = _tail(body, overlap)
        carry_start = body.rfind(carry) if carry else len(body)
        carry_spans = [
            (max(0, start - carry_start), end - carry_start, page)
            for start, end, page in cur_spans
            if end > carry_start
        ]
        cur, cur_len, cur_start, cur_end = [], 0, None, None
        cur_spans = []

    for para, page in paras:
        sep = 2 if cur else 0
        if cur and cur_len + sep + len(para) <= budget:
            cur_spans.append((cur_len + sep, cur_len + sep + len(para), page))
            cur.append(para)
            cur_len += sep + len(para)
            cur_end = page
            continue

        flush()
        if carry and len(carry) + 2 + len(para) <= budget:
            cur, cur_len, cur_start, cur_end = [carry, para], len(carry) + 2 + len(para), page, page
            cur_spans = carry_spans + [(len(carry) + 2, len(carry) + 2 + len(para), page)]
            continue
        if len(para) <= budget:
            cur, cur_len, cur_start, cur_end = [para], len(para), page, page
            cur_spans = [(0, len(para), page)]
            continue

        # Oversized paragraph: whitespace-aware sliding window.
        for piece in _windows(para, budget, overlap):
            out.append((emit(piece), page, page, sorted(prefix_pages | {page})))
        carry = _tail(para, overlap)
        carry_spans = [(0, len(carry), page)] if carry else []
    flush()
    return out


def _windows(text: str, size: int, overlap: int) -> list[str]:
    pieces: list[str] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + size, n)
        if end < n:
            ws = text.rfind(" ", start + size // 2, end)
            if ws > start:
                end = ws
        piece = text[start:end].strip()
        if piece:
            pieces.append(piece)
        if end >= n:
            break
        nxt = end - overlap if overlap else end
        if overlap:
            ws = text.find(" ", nxt, end)
            if ws != -1:
                nxt = ws + 1
        start = max(nxt, start + 1)
    return pieces


def _tail(text: str, overlap: int) -> str:
    if overlap <= 0 or len(text) <= overlap:
        return ""
    tail = text[-overlap:]
    ws = tail.find(" ")
    if 0 <= ws < len(tail) - 1:
        tail = tail[ws + 1 :]
    return tail.strip()
