"""Paragraph-aware chunker for extracted PDF text.

Strategy:
  1. Split each page on blank lines (paragraph boundaries).
  2. Greedily fill the current chunk up to `chunk_size` characters.
  3. If a single paragraph exceeds `chunk_size`, fall back to a sliding window with `overlap`
     characters of carryover so we do not cut mid-sentence at the boundary.
  4. Chunks never cross page boundaries; that keeps `payload.page` honest for citations.

Each chunk dict carries `{"text", "page", "index", "source"}`. `index` is a global counter so
`embed._stable_chunk_id` can hash it into an idempotent Qdrant point id.
"""

import re
from typing import Any

ChunkDict = dict[str, Any]


def chunk_pages(
    pages: list[tuple[int, str]],
    source: str,
    chunk_size: int = 1000,
    overlap: int = 100,
) -> list[ChunkDict]:
    chunks: list[ChunkDict] = []
    global_index = 0

    for page_num, text in pages:
        if not text or not text.strip():
            continue

        paragraphs = re.split(r"\n\s*\n", text.strip())
        current_chunk: list[str] = []
        current_len = 0

        for para in paragraphs:
            para = para.strip()
            if not para:
                continue

            # +1 accounts for the "\n\n" join inserted between paragraphs.
            if current_len + len(para) + 1 <= chunk_size:
                current_chunk.append(para)
                current_len += len(para) + 1
                continue

            if current_chunk:
                chunks.append(_chunk_dict("\n\n".join(current_chunk), page_num, global_index, source))
                global_index += 1

            if len(para) <= chunk_size:
                current_chunk = [para]
                current_len = len(para) + 1
            else:
                # Paragraph longer than chunk_size: sliding window with overlap.
                start = 0
                step = chunk_size - overlap
                while start < len(para):
                    end = min(start + chunk_size, len(para))
                    chunks.append(_chunk_dict(para[start:end], page_num, global_index, source))
                    global_index += 1
                    start += step
                current_chunk = []
                current_len = 0

        if current_chunk:
            chunks.append(_chunk_dict("\n\n".join(current_chunk), page_num, global_index, source))
            global_index += 1

    return chunks


def _chunk_dict(text: str, page: int, index: int, source: str) -> ChunkDict:
    return {"text": text, "page": page, "index": index, "source": source}
