"""Chunking strategy for extracted PDF text."""
import re
from typing import Any

ChunkDict = dict[str, Any]


def chunk_pages(
    pages: list[tuple[int, str]],
    source: str,
    chunk_size: int = 1000,
    overlap: int = 100,
) -> list[ChunkDict]:
    """
    Split per-page text into overlapping chunks suitable for embedding.

    Splits on paragraph boundaries when possible; falls back to fixed-size.

    Args:
        pages: List of (page_number, text) from extraction.
        source: Source identifier (e.g. filename or collection name).
        chunk_size: Target characters per chunk.
        overlap: Overlap between consecutive chunks.

    Returns:
        List of dicts: {"text", "page", "index", "source"}.
    """
    chunks: list[ChunkDict] = []
    global_index = 0

    for page_num, text in pages:
        if not text or not text.strip():
            continue

        # Split on double newlines (paragraphs) first
        paragraphs = re.split(r"\n\s*\n", text.strip())

        current_chunk = []
        current_len = 0

        for para in paragraphs:
            para = para.strip()
            if not para:
                continue

            if current_len + len(para) + 1 <= chunk_size:
                current_chunk.append(para)
                current_len += len(para) + 1
            else:
                if current_chunk:
                    chunk_text = "\n\n".join(current_chunk)
                    chunks.append({
                        "text": chunk_text,
                        "page": page_num,
                        "index": global_index,
                        "source": source,
                    })
                    global_index += 1

                if len(para) <= chunk_size:
                    current_chunk = [para]
                    current_len = len(para) + 1
                else:
                    # Fixed-size split for long paragraphs
                    start = 0
                    step = chunk_size - overlap
                    while start < len(para):
                        end = min(start + chunk_size, len(para))
                        chunk_text = para[start:end]
                        chunks.append({
                            "text": chunk_text,
                            "page": page_num,
                            "index": global_index,
                            "source": source,
                        })
                        global_index += 1
                        start += step

        if current_chunk:
            chunk_text = "\n\n".join(current_chunk)
            chunks.append({
                "text": chunk_text,
                "page": page_num,
                "index": global_index,
                "source": source,
            })
            global_index += 1

    return chunks
