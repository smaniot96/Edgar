"""Uncached availability checks for explicitly bound adventure sources.

Metadata is not a readiness proof. Verify the live vector schema and usable text coverage;
scroll text payloads only, never download the corpus's full embedding vectors.
"""

from __future__ import annotations

from typing import Any

from qdrant_client.models import Filter, HasVectorCondition, VectorParams

from db.vector.client import get_qdrant_client
from db.vector.embeddings import VECTOR_SIZE
from db.vector.versions import active_index_read


def inspect_source(
    slug: str,
    *,
    expected_chunks: int | None = None,
    expected_source_hash: str | None = None,
    expected_extraction_hash: str | None = None,
    expected_pages: list[int] | None = None,
    client: Any = None,
) -> dict:
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
        state = {}
        with active_index_read(q, slug, state_out=state) as index_filter:
            entry = state["sources"].get(slug)
            if entry is None and len(state["sources"]) == 1:
                entry = next(iter(state["sources"].values()))
            if entry and entry.get("active"):
                result["index_version"] = entry["active"]["index_version"]
            return _inspect_active_source(
                q,
                slug,
                result,
                index_filter,
                expected_chunks,
                expected_source_hash,
                expected_extraction_hash,
                expected_pages,
            )
    except Exception:
        # An outage is not evidence that a collection was deleted.
        result["status"] = "unavailable"
    return result


def _inspect_active_source(
    q,
    slug,
    result,
    index_filter,
    expected_chunks,
    expected_source_hash,
    expected_extraction_hash,
    expected_pages,
):
    total = q.count(collection_name=slug, count_filter=index_filter, exact=True).count
    offset = None
    mismatched = False
    covered_pages: set[int] = set()
    payload_fields = ["text"]
    if expected_source_hash or expected_extraction_hash or expected_pages is not None:
        payload_fields += ["source_sha256", "extraction_sha256", "source_pages"]
    vector_filter = Filter(
        must=[
            HasVectorCondition(has_vector=""),
            *([index_filter] if index_filter is not None else []),
        ]
    )
    while True:
        points, offset = q.scroll(
            collection_name=slug,
            limit=256,
            offset=offset,
            scroll_filter=vector_filter,
            with_payload=payload_fields,
            with_vectors=False,
        )
        for point in points:
            payload = point.payload or {}
            text = payload.get("text")
            if isinstance(text, str) and text.strip():
                result["usable_chunks"] += 1
                matches = (
                    not expected_source_hash or payload.get("source_sha256") == expected_source_hash
                ) and (
                    not expected_extraction_hash
                    or payload.get("extraction_sha256") == expected_extraction_hash
                )
                mismatched |= not matches
                pages = payload.get("source_pages")
                if matches and isinstance(pages, list):
                    covered_pages.update(
                        n for n in pages if isinstance(n, int) and n in (expected_pages or [])
                    )
        if offset is None:
            break
    usable = result["usable_chunks"]
    result["status"] = (
        "empty"
        if not usable
        else (
            "partial"
            if usable < total
            or (expected_chunks and usable < expected_chunks)
            or (expected_source_hash and expected_chunks and usable != expected_chunks)
            or mismatched
            or (expected_pages is not None and not set(expected_pages).issubset(covered_pages))
            else "ready"
        )
    )
    return result
