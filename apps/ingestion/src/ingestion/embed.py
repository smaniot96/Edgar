"""Extract -> chunk -> embed -> upsert a PDF into a Qdrant collection.

Idempotency: each chunk's Qdrant point id is a stable hash of (source, page, index, prefix).
Re-running the same PDF into the same collection upserts the same ids, so we never duplicate
points; you can re-ingest after fixing a bug or updating a PDF without cleanup.

Batching: embeddings are computed `BATCH_SIZE` chunks at a time with a small `BATCH_DELAY_SEC`
between batches as a courtesy to OpenAI rate limits. The retry policy lives in
`db.vector.embeddings`; here we just propagate failures after logging which batch died.
"""

import hashlib
import time
from pathlib import Path

from loguru import logger
from qdrant_client.models import PointStruct

from db.vector import VECTOR_SIZE, get_embeddings, get_qdrant_client
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

    client = get_qdrant_client()

    # Idempotent collection create. Cosine matches our embedding model normalisation.
    if not any(c.name == collection_name for c in client.get_collections().collections):
        client.create_collection(
            collection_name=collection_name,
            vectors_config={"size": VECTOR_SIZE, "distance": "Cosine"},
        )
        logger.info("Created collection {}", collection_name)

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
                payload={"text": c["text"], "page": c["page"], "source": c["source"]},
            )
            for j, c in enumerate(batch)
        ]

        try:
            client.upsert(collection_name=collection_name, points=points)
        except Exception as e:
            logger.error("Upsert failed for batch {}: {}", i, e)
            raise

        total_ingested += len(batch)
        logger.info("Upserted batch {}-{} ({} chunks)", i, i + len(batch), total_ingested)

        # Skip the delay after the final batch.
        if i + BATCH_SIZE < len(chunks):
            time.sleep(BATCH_DELAY_SEC)

    return total_ingested
