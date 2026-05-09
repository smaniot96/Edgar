"""Rules vs adventure retrieval helpers shared by the agent and the rules_engine CLI.

The split exists so the adjudicator rules on canonical books only (PHB/DMG/MM) and the
narrator can pull in module-specific story text. The two functions return the same chunk
shape (`{text, source, page, kind, collection}`) so callers can merge or label them as needed.

Missing collections are skipped silently: a campaign that has not ingested a particular
module should not hard-fail retrieval.
"""

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


def search_collections(
    query_text: str,
    collection_names: list[str],
    *,
    limit_per_collection: int = 5,
    kind: str = "rules",
    client: QdrantClient | None = None,
) -> list[dict[str, Any]]:
    """Embed once, search each collection, return merged hits in collection order."""
    if not query_text.strip() or not collection_names:
        return []

    q = _client(client)
    vector = get_embeddings([query_text])[0]

    out: list[dict[str, Any]] = []
    for collection_name in collection_names:
        try:
            results = q.search(
                collection_name=collection_name,
                query_vector=vector,
                limit=limit_per_collection,
                with_payload=True,
            )
        except Exception:
            # Most likely "collection not found"; treat as empty result.
            continue
        for r in results:
            payload = r.payload or {}
            chunk = _chunk_from_payload(payload, kind=kind)
            chunk["collection"] = collection_name
            out.append(chunk)
    return out


def search_rules_context(
    query_text: str,
    *,
    limit_per_collection: int = 5,
    include_legacy_collections: bool = True,
    client: QdrantClient | None = None,
) -> list[dict[str, Any]]:
    """PHB + DMG + MM (canonical `rules_*` plus the legacy aliases by default)."""
    names = effective_rules_collection_names(include_legacy=include_legacy_collections)
    return search_collections(
        query_text, names, limit_per_collection=limit_per_collection, kind="rules", client=client
    )


def search_adventure_context(
    query_text: str,
    adventure_collections: list[str] | None,
    *,
    campaign_id: int | None = None,
    limit_per_collection: int = 5,
    client: QdrantClient | None = None,
) -> list[dict[str, Any]]:
    """Bound adventure modules plus the per-campaign lore collection if a campaign id is set."""
    names = list(effective_adventure_collection_names(adventure_collections))
    if campaign_id is not None:
        lore = campaign_lore_collection(campaign_id)
        if lore not in names:
            names.append(lore)
    return search_collections(
        query_text, names, limit_per_collection=limit_per_collection, kind="adventure", client=client
    )


def _chunk_from_payload(payload: dict[str, Any], *, kind: str) -> dict[str, Any]:
    return {
        "text": payload.get("text", ""),
        "source": payload.get("source", ""),
        "page": payload.get("page"),
        "kind": kind,
    }


def _client(client: QdrantClient | None) -> QdrantClient:
    return client if client is not None else get_qdrant_client()
