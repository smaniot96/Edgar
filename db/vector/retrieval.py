"""Shared Qdrant RAG search helpers (rules vs adventure modules)."""

from __future__ import annotations

from typing import Any

from qdrant_client import QdrantClient

from db.vector.client import get_qdrant_client
from db.vector.collections import (
    campaign_lore_collection,
    effective_adventure_collection_names,
    effective_rules_collection_names,
)
from db.vector.embeddings import get_embeddings


def _chunk_from_payload(payload: dict[str, Any], *, kind: str) -> dict[str, Any]:
    return {
        "text": payload.get("text", ""),
        "source": payload.get("source", ""),
        "page": payload.get("page"),
        "kind": kind,
    }


def search_collections(
    query_text: str,
    collection_names: list[str],
    *,
    limit_per_collection: int = 5,
    kind: str = "rules",
    client: QdrantClient | None = None,
) -> list[dict[str, Any]]:
    """
    Embed query_text once and search each collection; merge hits in order.

    Missing collections are skipped (no error).
    """
    if not query_text.strip() or not collection_names:
        return []

    q = _client(client)
    embeddings = get_embeddings([query_text])
    vector = embeddings[0]

    out: list[dict[str, Any]] = []
    for collection_name in collection_names:
        try:
            results = q.search(
                collection_name=collection_name,
                query_vector=vector,
                limit=limit_per_collection,
                with_payload=True,
            )
            for r in results:
                payload = r.payload or {}
                chunk = _chunk_from_payload(payload, kind=kind)
                chunk["collection"] = collection_name
                out.append(chunk)
        except Exception:
            continue
    return out


def search_rules_context(
    query_text: str,
    *,
    limit_per_collection: int = 5,
    include_legacy_collections: bool = True,
    client: QdrantClient | None = None,
) -> list[dict[str, Any]]:
    """Search PHB, DMG, MM (rules_* names plus optional legacy aliases)."""
    names = effective_rules_collection_names(include_legacy=include_legacy_collections)
    return search_collections(
        query_text,
        names,
        limit_per_collection=limit_per_collection,
        kind="rules",
        client=client,
    )


def search_adventure_context(
    query_text: str,
    adventure_collections: list[str] | None,
    *,
    campaign_id: int | None = None,
    limit_per_collection: int = 5,
    client: QdrantClient | None = None,
) -> list[dict[str, Any]]:
    """Search bound adventure modules + optional campaign_lore_{id} collection."""
    names = list(effective_adventure_collection_names(adventure_collections))
    if campaign_id is not None:
        lore = campaign_lore_collection(campaign_id)
        if lore not in names:
            names.append(lore)
    return search_collections(
        query_text,
        names,
        limit_per_collection=limit_per_collection,
        kind="adventure",
        client=client,
    )


def _client(client: QdrantClient | None) -> QdrantClient:
    return client if client is not None else get_qdrant_client()
