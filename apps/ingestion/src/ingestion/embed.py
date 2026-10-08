"""Extract -> chunk -> embed -> upsert a PDF into a Qdrant collection.

Idempotency: each chunk's Qdrant point id is a stable hash of (source, page, index, prefix).
Re-running the same PDF into the same collection upserts the same ids, so we never duplicate
points; you can re-ingest after fixing a bug or updating a PDF without cleanup.

Re-chunking caveat: point ids depend on the chunk boundaries, so after a chunker change
re-ingesting into an existing collection would leave the old points next to the new ones.
Pass `replace=True` (CLI `--replace`): once every new chunk has been upserted successfully,
points of the *same source* that this run did not write are deleted. Nothing is removed if
embedding/upsert fails midway, and other sources sharing the collection are untouched.

Payload: `{text, page, page_end, source, chunk_index, section, chapter, heading_path,
stat_block}`; retrieval treats every field except `text`/`page`/`source` as optional so older
collections keep working. A payload index on `page` is created with the collection so the
session intro can read the first pages in order (`order_by=page`).

Batching: embeddings are computed `BATCH_SIZE` chunks at a time with a small `BATCH_DELAY_SEC`
between batches as a courtesy to OpenAI rate limits. The retry policy lives in
`db.vector.embeddings`; here we just propagate failures after logging which batch died.
"""

import hashlib
import time
from pathlib import Path

from loguru import logger
from qdrant_client.models import (
    FieldCondition,
    Filter,
    MatchValue,
    PayloadSchemaType,
    PointIdsList,
    PointStruct,
)

from db.vector import VECTOR_SIZE, get_embeddings, get_qdrant_client, invalidate_collection_cache
from edgar_core.config import EDGAR_ROOT
from ingestion.chunk import chunk_pages
from ingestion.extract import extract_and_save_markdown, extract_text_by_page

BATCH_SIZE = 50
BATCH_DELAY_SEC = 0.5


def _stable_chunk_id(source: str, page: int, index: int, text: str) -> int:
    """64-bit int derived from sha256 of identity fields. Collisions are astronomically rare."""
    h = hashlib.sha256(f"{source}:{page}:{index}:{text[:100]}".encode()).hexdigest()
    return int(h[:16], 16)


def _default_markdown_dir() -> Path:
    """`/output/markdown` when the Docker bind mount exists, otherwise `Edgar/data/markdown`."""
    if Path("/output/markdown").exists():
        return Path("/output/markdown")
    return EDGAR_ROOT / "data" / "markdown"


def run_pipeline(
    pdf_path: str | Path,
    collection_name: str,
    chunk_size: int = 1000,
    overlap: int = 100,
    save_markdown: bool = True,
    markdown_dir: Path | None = None,
    replace: bool = False,
) -> int:
    """Returns the number of chunks ingested. Returns 0 on empty PDFs (no error)."""
    pdf_path = Path(pdf_path)
    source = pdf_path.stem

    pages = extract_text_by_page(pdf_path)
    if not pages:
        logger.warning("No text extracted from {}", pdf_path)
        return 0

    if save_markdown:
        out_dir = markdown_dir or _default_markdown_dir()
        out_path = extract_and_save_markdown(pdf_path, out_dir)
        logger.info("Saved markdown to {}", out_path)

    chunks = chunk_pages(pages, source=source, chunk_size=chunk_size, overlap=overlap)
    if not chunks:
        logger.warning("No chunks produced from {}", pdf_path)
        return 0

    return _embed_and_upsert(chunks, collection_name, replace=replace)


def ingest_pages(
    pages: list[tuple[int, str]],
    source: str,
    collection_name: str,
    chunk_size: int = 1000,
    overlap: int = 100,
    replace: bool = False,
) -> int:
    """Chunk + embed + upsert already-extracted `(page, text)` pages (e.g. AI-generated text).

    Mirrors `run_pipeline` but skips the PDF extraction step, so callers that already have text
    (a generated campaign, a markdown doc) can reuse the same chunking/embedding/idempotency.
    """
    chunks = chunk_pages(pages, source=source, chunk_size=chunk_size, overlap=overlap)
    if not chunks:
        return 0
    return _embed_and_upsert(chunks, collection_name, replace=replace)


def _chunk_payload(c: dict) -> dict:
    payload = {
        "text": c["text"],
        "page": c["page"],
        "source": c["source"],
        "chunk_index": c["index"],
    }
    for key in ("page_end", "section", "chapter", "heading_path", "stat_block"):
        if c.get(key) is not None:
            payload[key] = c[key]
    return payload


def _ensure_collection(client, collection_name: str) -> None:
    """Create the collection (Cosine, VECTOR_SIZE) if missing; ensure the `page` payload index."""
    if not any(c.name == collection_name for c in client.get_collections().collections):
        client.create_collection(
            collection_name=collection_name,
            vectors_config={"size": VECTOR_SIZE, "distance": "Cosine"},
        )
        logger.info("Created collection {}", collection_name)
        invalidate_collection_cache()
    try:
        # Idempotent; lets the session intro scroll the first pages with order_by=page.
        client.create_payload_index(
            collection_name=collection_name,
            field_name="page",
            field_schema=PayloadSchemaType.INTEGER,
        )
    except Exception as e:  # index is an optimisation only
        logger.warning("Could not create page index on {}: {}: {}", collection_name, type(e).__name__, e)


def _prune_stale(client, collection_name: str, source: str, keep_ids: set[int]) -> int:
    """Delete points of `source` in the collection whose id is not in `keep_ids`."""
    stale: list = []
    offset = None
    source_filter = Filter(must=[FieldCondition(key="source", match=MatchValue(value=source))])
    while True:
        points, offset = client.scroll(
            collection_name=collection_name,
            scroll_filter=source_filter,
            limit=1000,
            offset=offset,
            with_payload=False,
            with_vectors=False,
        )
        stale.extend(p.id for p in points if p.id not in keep_ids)
        if offset is None:
            break
    for i in range(0, len(stale), 1000):
        client.delete(
            collection_name=collection_name,
            points_selector=PointIdsList(points=stale[i : i + 1000]),
        )
    return len(stale)


def _embed_and_upsert(chunks: list[dict], collection_name: str, *, replace: bool = False) -> int:
    """Embed chunks in batches and upsert into Qdrant; create the collection if missing.

    With `replace=True`, stale points of the same source(s) are pruned after a full success.
    """
    client = get_qdrant_client()
    _ensure_collection(client, collection_name)
    written_ids: set[int] = set()

    total_ingested = 0
    for i in range(0, len(chunks), BATCH_SIZE):
        batch = chunks[i : i + BATCH_SIZE]
        texts = [c["text"] for c in batch]

        try:
            embeddings = get_embeddings(texts)
        except Exception as e:
            logger.error("Embedding failed for batch {}: {}", i, e)
            raise

        points = [
            PointStruct(
                id=_stable_chunk_id(c["source"], c["page"], c["index"], c["text"]),
                vector=embeddings[j],
                payload=_chunk_payload(c),
            )
            for j, c in enumerate(batch)
        ]

        try:
            client.upsert(collection_name=collection_name, points=points)
        except Exception as e:
            logger.error("Upsert failed for batch {}: {}", i, e)
            raise

        written_ids.update(p.id for p in points)
        total_ingested += len(batch)
        logger.info("Upserted batch {}-{} ({} chunks)", i, i + len(batch), total_ingested)

        # Skip the delay after the final batch.
        if i + BATCH_SIZE < len(chunks):
            time.sleep(BATCH_DELAY_SEC)

    if replace:
        for source in sorted({c["source"] for c in chunks}):
            n = _prune_stale(client, collection_name, source, written_ids)
            logger.info("Pruned {} stale points of source {} from {}", n, source, collection_name)

    return total_ingested
