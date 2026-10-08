"""Rules vs adventure retrieval helpers shared by the agent and the rules_engine CLI.

The split exists so the adjudicator rules on canonical books only (PHB/DMG/MM) and the
narrator can pull in module-specific story text. Every function returns the same chunk shape
(`{text, source, page, kind, collection, score}`, plus `section` / `chapter` / `heading_path`
when the payload was produced by the heading-aware chunker) so callers can merge or label them.

Ranking: the query is embedded once (memoised across the turn by `embed_query`), every
*existing* candidate collection is searched for `limit_per_collection` hits, and the union is
sorted globally by score, filtered by `min_score`, de-duplicated (exact or near-identical text,
e.g. the same PDF ingested under a legacy and a canonical name) and capped at `limit`. Callers
therefore get the best chunks first regardless of which book they came from.

Errors: collections that do not exist are skipped *before* querying (cached collection list).
A collection that fails to query is logged as a warning with the collection name and exception
type; if every queried collection fails, `RetrievalError` is raised so an outage surfaces
instead of silently producing an empty context.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from qdrant_client import QdrantClient

from db.vector.client import get_qdrant_client
from db.vector.collections import (
    MONSTER_MANUAL_COLLECTIONS,
    campaign_lore_collection,
    effective_adventure_collection_names,
    effective_rules_collection_names,
    existing_collection_names,
)
from db.vector.embeddings import embed_query, get_embeddings  # noqa: F401  (re-exported)

logger = logging.getLogger(__name__)

# Cosine similarity floor for text-embedding-3-small: unrelated passages typically sit below ~0.2.
DEFAULT_MIN_SCORE = 0.2
DEFAULT_SEARCH_LIMIT = 10
DEFAULT_RULES_LIMIT = 6
DEFAULT_ADVENTURE_LIMIT = 8
DEFAULT_MONSTER_LIMIT = 2
# Word-set Jaccard similarity at or above which two chunks count as duplicates.
NEAR_DUPLICATE_JACCARD = 0.85

_OPTIONAL_PAYLOAD_KEYS = ("section", "chapter", "heading_path", "page_end", "stat_block")


class RetrievalError(RuntimeError):
    """Every queried collection failed (Qdrant unreachable, schema mismatch, ...)."""


def search_collections(
    query_text: str,
    collection_names: list[str],
    *,
    limit_per_collection: int = 5,
    kind: str = "rules",
    client: QdrantClient | None = None,
    limit: int | None = DEFAULT_SEARCH_LIMIT,
    min_score: float | None = DEFAULT_MIN_SCORE,
    dedupe: bool = True,
) -> list[dict[str, Any]]:
    """Embed once, search each existing collection, return hits ranked globally by score.

    `limit=None` disables the total cap, `min_score=None` disables the threshold.
    """
    if not query_text.strip() or not collection_names:
        return []

    q = _client(client)
    names = _existing_only(q, collection_names)
    if not names:
        return []

    vector = embed_query(query_text)
    hits = _gather_hits(q, names, vector, limit_per_collection, kind=kind)
    return _rank(hits, limit=limit, min_score=min_score, dedupe=dedupe)


def search_rules_context(
    query_text: str,
    *,
    limit_per_collection: int = 5,
    include_legacy_collections: bool = True,
    client: QdrantClient | None = None,
    limit: int | None = DEFAULT_RULES_LIMIT,
    min_score: float | None = DEFAULT_MIN_SCORE,
) -> list[dict[str, Any]]:
    """PHB + DMG + MM (canonical `rules_*` plus legacy aliases that exist), best first."""
    names = effective_rules_collection_names(include_legacy=include_legacy_collections)
    return search_collections(
        query_text,
        names,
        limit_per_collection=limit_per_collection,
        kind="rules",
        client=client,
        limit=limit,
        min_score=min_score,
    )


def search_adventure_context(
    query_text: str,
    adventure_collections: list[str] | None,
    *,
    campaign_id: int | None = None,
    limit_per_collection: int = 5,
    client: QdrantClient | None = None,
    limit: int | None = DEFAULT_ADVENTURE_LIMIT,
    min_score: float | None = DEFAULT_MIN_SCORE,
) -> list[dict[str, Any]]:
    """Bound adventure modules plus the per-campaign lore collection (if it exists), best first."""
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
        limit=limit,
        min_score=min_score,
    )


def retrieve_monster(
    name: str,
    *,
    client: QdrantClient | None = None,
    limit: int = DEFAULT_MONSTER_LIMIT,
    min_score: float | None = 0.15,
    collections: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Monster Manual chunks for a creature name (e.g. a combat target), stat blocks first.

    Searches only the monster-manual collections (`rules_monster_manual`, legacy
    `monster_manual`). Vector hits are re-ranked so chunks whose heading / text actually name
    the creature, and chunks that look like stat blocks, beat merely similar lore. Each chunk
    carries `kind="rules"`, the raw `score` and the re-ranked `rank_score`.
    """
    clean = _monster_key(name)
    if not clean:
        return []
    q = _client(client)
    names = _existing_only(q, list(collections or MONSTER_MANUAL_COLLECTIONS))
    if not names:
        return []

    vector = embed_query(f"{name.strip()} stat block: Armor Class, Hit Points, Speed, Challenge, Actions")
    hits = _gather_hits(q, names, vector, max(10, limit * 5), kind="rules")
    if min_score is not None:
        hits = [h for h in hits if h.get("score") is None or h["score"] >= min_score]

    for h in hits:
        h["rank_score"] = (h.get("score") or 0.0) + _monster_bonus(h, clean)
    hits.sort(key=lambda h: h["rank_score"], reverse=True)
    return _rank(hits, limit=limit, min_score=None, dedupe=True, presorted=True)


# --- internals -------------------------------------------------------------------------------


def _existing_only(q: Any, collection_names: list[str]) -> list[str]:
    """Drop duplicates and collections that do not exist (when the list can be fetched)."""
    names = list(dict.fromkeys(collection_names))
    existing = existing_collection_names(q)
    if existing is None:
        return names
    present = [n for n in names if n in existing]
    skipped = [n for n in names if n not in existing]
    if skipped:
        logger.debug("retrieval: skipping missing collections %s", skipped)
    return present


def _gather_hits(
    q: Any, names: list[str], vector: list[float], limit_per_collection: int, *, kind: str
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    failures = 0
    last_error: Exception | None = None
    for collection_name in names:
        try:
            results = _query_collection(q, collection_name, vector, limit_per_collection)
        except Exception as e:
            failures += 1
            last_error = e
            logger.warning(
                "retrieval: query failed for collection=%s (%s: %s)",
                collection_name,
                type(e).__name__,
                e,
            )
            continue
        for r in results:
            payload = getattr(r, "payload", None) or {}
            chunk = _chunk_from_payload(payload, kind=kind)
            chunk["collection"] = collection_name
            chunk["score"] = getattr(r, "score", None)
            out.append(chunk)
    if names and failures == len(names):
        raise RetrievalError(
            f"all {failures} Qdrant collection queries failed; last error "
            f"{type(last_error).__name__}: {last_error}"
        ) from last_error
    return out


def _rank(
    hits: list[dict[str, Any]],
    *,
    limit: int | None,
    min_score: float | None,
    dedupe: bool,
    presorted: bool = False,
) -> list[dict[str, Any]]:
    if min_score is not None:
        hits = [h for h in hits if h.get("score") is None or h["score"] >= min_score]
    if not presorted:
        # Stable sort: equal scores keep collection order. Missing scores sink to the bottom.
        hits = sorted(hits, key=lambda h: h.get("score") if h.get("score") is not None else float("-inf"), reverse=True)

    out: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    seen_words: list[frozenset[str]] = []
    for h in hits:
        if limit is not None and len(out) >= limit:
            break
        if dedupe:
            key = _normalize(h.get("text", ""))
            if not key:
                continue
            if key in seen_keys:
                continue
            words = frozenset(key.split())
            if any(_jaccard(words, w) >= NEAR_DUPLICATE_JACCARD for w in seen_words):
                continue
            seen_keys.add(key)
            seen_words.append(words)
        out.append(h)
    return out


def _normalize(text: str) -> str:
    return re.sub(r"\W+", " ", text.lower()).strip()


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _monster_key(name: str) -> str:
    """'Goblins #2' -> 'goblin'; plural 's' dropped so substring matching tolerates plurals."""
    key = re.sub(r"[^a-z ]+", " ", name.lower())
    key = re.sub(r"\s+", " ", key).strip()
    if len(key) > 3 and key.endswith("s"):
        key = key[:-1]
    return key


def _monster_bonus(chunk: dict[str, Any], key: str) -> float:
    bonus = 0.0
    heading = _normalize(f"{chunk.get('section') or ''} {chunk.get('heading_path') or ''}")
    text = _normalize(chunk.get("text", ""))
    if key in heading:
        bonus += 0.15
    elif key in text[:300]:
        bonus += 0.08
    elif key in text:
        bonus += 0.04
    raw = chunk.get("text", "")
    if chunk.get("stat_block") or ("Armor Class" in raw and ("Hit Points" in raw or "Challenge" in raw)):
        bonus += 0.05
    return bonus


def _query_collection(q: QdrantClient, collection_name: str, vector, limit: int):
    """Run a vector search, preferring the modern `query_points` API.

    qdrant-client >= 1.12 removed the legacy `Client.search` method in favour of
    `query_points`, which returns a response object with a `.points` list. We fall back to
    the old `search` for older clients so the helper works across versions.
    """
    if hasattr(q, "query_points"):
        response = q.query_points(
            collection_name=collection_name,
            query=vector,
            limit=limit,
            with_payload=True,
        )
        return response.points
    return q.search(
        collection_name=collection_name,
        query_vector=vector,
        limit=limit,
        with_payload=True,
    )


def _chunk_from_payload(payload: dict[str, Any], *, kind: str) -> dict[str, Any]:
    chunk: dict[str, Any] = {
        "text": payload.get("text", ""),
        "source": payload.get("source", ""),
        "page": payload.get("page"),
        "kind": kind,
    }
    # Heading-aware payloads (newer ingests) carry structure; older collections do not.
    for key in _OPTIONAL_PAYLOAD_KEYS:
        if payload.get(key) is not None:
            chunk[key] = payload[key]
    return chunk


def _client(client: QdrantClient | None) -> QdrantClient:
    return client if client is not None else get_qdrant_client()
