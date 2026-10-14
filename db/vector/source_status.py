"""Uncached availability checks for explicitly bound adventure sources.

Metadata is not a readiness proof. Verify the live vector schema and usable text coverage;
scroll text payloads only, never download the corpus's full embedding vectors.
"""

from __future__ import annotations

from typing import Any

from qdrant_client.models import Filter, HasVectorCondition, VectorParams

from db.vector.client import get_qdrant_client
from db.vector.embeddings import VECTOR_SIZE


def inspect_source(slug: str, *, expected_chunks: int | None = None, client: Any = None) -> dict:
    result = {
        "slug": slug,
        "status": "unavailable",
        "usable_chunks": 0,
        "expected_chunks": expected_chunks,
        "collection_exists": False,
    }
    try:
        q = client if client is not None else get_qdrant_client()
        if not q.collection_exists(slug):
            return {**result, "status": "missing"}
        result["collection_exists"] = True
        vectors = q.get_collection(slug).config.params.vectors
        # Retrieval queries the unnamed dense vector. A differently shaped collection is not
        # usable through that contract even if it contains text and other vectors.
        if not isinstance(vectors, VectorParams) or vectors.size != VECTOR_SIZE:
            return {**result, "status": "empty"}
        total = q.count(collection_name=slug, exact=True).count
        offset = None
        vector_filter = Filter(must=[HasVectorCondition(has_vector="")])
        while True:
            points, offset = q.scroll(
                collection_name=slug,
                limit=256,
                offset=offset,
                scroll_filter=vector_filter,
                with_payload=["text"],
                with_vectors=False,
            )
            for point in points:
                text = (point.payload or {}).get("text")
                if isinstance(text, str) and text.strip():
                    result["usable_chunks"] += 1
            if offset is None:
                break
        usable = result["usable_chunks"]
        result["status"] = (
            "empty"
            if not usable
            else (
                "partial"
                if usable < total or (expected_chunks and usable < expected_chunks)
                else "ready"
            )
        )
    except Exception:
        # An outage is not evidence that a collection was deleted.
        result["status"] = "unavailable"
    return result
