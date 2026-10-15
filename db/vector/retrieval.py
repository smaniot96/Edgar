"""Rules vs adventure retrieval helpers shared by the agent and the rules_engine CLI.

The split exists so the adjudicator rules on canonical books only (PHB/DMG/MM) and the
narrator can pull in module-specific story text. Every function returns the same chunk shape
(`{text, source, page, kind, collection, score}`, plus `section` / `chapter` / `heading_path`
when the payload was produced by the heading-aware chunker) so callers can merge or label them.

General search: the query is embedded once (memoised across the turn by `embed_query`), every
*existing* candidate collection is searched for `limit_per_collection` hits, and the union is
sorted globally by score, filtered by `min_score`, de-duplicated (exact or near-identical text,
e.g. the same PDF ingested under a legacy and a canonical name) and capped at `limit`. Callers
therefore get the best chunks first regardless of which book they came from. Targeted rules
queries additionally use exact headings/lexical fallback, exclude unrelated hits and optional
material, and expand only verified same-section continuations.

Errors: collections that do not exist are skipped *before* querying (cached collection list).
A collection that fails to query is logged as a warning with the collection name and exception
type; if every queried collection fails, `RetrievalError` is raised so an outage surfaces
instead of silently producing an empty context. Module play additionally requires every bound
collection to exist and query successfully; optional rules aliases and campaign lore are tolerant.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from qdrant_client import QdrantClient
from qdrant_client.models import FieldCondition, Filter, MatchAny, MatchText, MatchValue

from db.vector.client import get_qdrant_client
from db.vector.collections import (
    MONSTER_MANUAL_COLLECTIONS,
    campaign_lore_collection,
    effective_adventure_collection_names,
    effective_rules_collection_names,
    existing_collection_names,
)
from db.vector.embeddings import embed_query, get_embeddings  # noqa: F401  (re-exported)
from db.vector.references import normalize_reference, reference_matches
from db.vector.versions import active_index_read
from edgar_core.artifacts import text_digest
from edgar_core.manifests import ManifestError, require_published, select_scene_context

logger = logging.getLogger(__name__)

# Cosine similarity floor for text-embedding-3-small: unrelated passages typically sit below ~0.2.
DEFAULT_MIN_SCORE = 0.2
DEFAULT_SEARCH_LIMIT = 10
DEFAULT_RULES_LIMIT = 6
DEFAULT_ADVENTURE_LIMIT = 8
DEFAULT_MONSTER_LIMIT = 2
# Word-set Jaccard similarity at or above which two chunks count as duplicates.
NEAR_DUPLICATE_JACCARD = 0.85

_OPTIONAL_PAYLOAD_KEYS = (
    "section",
    "chapter",
    "heading_path",
    "page_end",
    "stat_block",
    "source_pages",
    "source_sha256",
    "extraction_sha256",
    "rules_edition",
    "chunk_index",
    "manifest_sha256",
    "passage_id",
    "scene_id",
    "scene",
    "scene_ids",
    "entity_ids",
    "audience",
    "requires",
    "span_start",
    "span_end",
    "text_sha256",
    "source_version",
    "index_version",
    "rule_policy",
)


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
    required_collections: list[str] | None = None,
    rules_edition: str | None = None,
) -> list[dict[str, Any]]:
    """Embed once, search each existing collection, return hits ranked globally by score.

    `limit=None` disables the total cap, `min_score=None` disables the threshold.
    Required collections use a fresh inventory and fail explicitly if missing or unqueryable.
    """
    if not query_text.strip() or not collection_names:
        return []

    q = _client(client)
    names = _existing_only(q, collection_names, refresh=bool(required_collections))
    missing_required = set(required_collections or []) - set(names)
    if missing_required:
        raise RetrievalError(
            f"required adventure collections are missing: {sorted(missing_required)}"
        )
    if not names:
        return []

    vector = embed_query(query_text)
    hits = _gather_hits(
        q,
        names,
        vector,
        limit_per_collection,
        kind=kind,
        required_collections=required_collections,
        rules_edition=rules_edition,
    )
    if rules_edition:
        hits = [c for c in hits if c.get("rules_edition") in (None, "unspecified", rules_edition)]
    return _rank(hits, limit=limit, min_score=min_score, dedupe=dedupe)


def search_rules_context(
    query_text: str,
    *,
    limit_per_collection: int = 5,
    include_legacy_collections: bool = True,
    client: QdrantClient | None = None,
    limit: int | None = DEFAULT_RULES_LIMIT,
    min_score: float | None = DEFAULT_MIN_SCORE,
    rules_edition: str | None = None,
    references: list[str] | None = None,
    include_optional: bool = False,
) -> list[dict[str, Any]]:
    """Core-book evidence, optionally targeted to independent named rule references.

    Targeted queries combine vector candidates with a bounded lexical lookup. Exact
    headings beat mentions, and unrelated vector hits cannot fill a targeted query. A
    matching continuation may be added only from the same section and source lineage.
    Optional/variant sections are labelled and excluded unless explicitly requested.
    """
    if not query_text.strip():
        return []
    refs = list(dict.fromkeys(r.strip() for r in references or [] if r.strip()))[:4]
    names = effective_rules_collection_names(include_legacy=include_legacy_collections)
    vector_error = None
    try:
        hits = search_collections(
            query_text,
            names,
            limit_per_collection=limit_per_collection,
            kind="rules",
            client=client,
            limit=None,
            min_score=None,
            dedupe=False,
            rules_edition=rules_edition,
        )
    except Exception as exc:
        if not refs:
            raise
        vector_error = exc
        hits = []
    q = _client(client)
    if refs:
        for name in _existing_only(q, names):
            hits.extend(_lookup_rule_candidates(q, name, refs, rules_edition))
    if vector_error is not None and not hits:
        raise RetrievalError(
            "Vector retrieval failed and no exact rule evidence was available."
        ) from vector_error
    candidates = []
    for hit in hits:
        policy = rule_policy(hit)
        if not include_optional and policy != "core":
            continue
        hit = {"retrieval_method": "vector", **hit, "rule_policy": policy}
        match = _rule_reference_match(hit, refs) if refs else 0
        if refs and not match:
            continue
        if min_score is not None and (hit.get("score") or 0) < min_score and match != 2:
            # A verified lexical lookup has no vector score; it is independent evidence.
            if hit.get("retrieval_method") != "lexical":
                continue
        hit["reference_match"] = "exact" if match == 2 else "lexical" if match else "semantic"
        candidates.append(hit)
    candidates.sort(
        key=lambda c: (
            {"exact": 2, "lexical": 1, "semantic": 0}[c["reference_match"]],
            bool(rules_edition and c.get("rules_edition") == rules_edition),
            c.get("score") or 0,
        ),
        reverse=True,
    )
    seeds = _rank(candidates, limit=limit, min_score=None, dedupe=True, presorted=True)
    if not refs:
        return seeds
    expanded = []
    for seed in seeds:
        expanded.append(seed)
        # At most one continuation for each leading exact reference; never page adjacency.
        if seed["reference_match"] == "exact" and len(expanded) < (limit or DEFAULT_RULES_LIMIT):
            expanded.extend(_rule_neighbors(q, seed, include_optional=include_optional))
    return _rank(expanded, limit=limit, min_score=None, dedupe=True, presorted=True)


def rule_policy(chunk: dict[str, Any]) -> str:
    """Explicit classification wins; conservative heading markers cover legacy payloads."""
    declared = chunk.get("rule_policy")
    if declared in ("optional", "variant"):
        return declared
    headings = " ".join(str(chunk.get(k) or "") for k in ("chapter", "section", "heading_path"))
    # A legacy sidebar may carry its title only in the leading text line.
    headings += " " + str(chunk.get("text") or "").split("\n", 1)[0]
    normalized = normalize_reference(headings)
    if re.search(r"\b(variant|variants|variante|varianti)\b", normalized):
        return "variant"
    if re.search(r"\b(optional|opzionale|opzionali)\b", normalized) or any(
        title in normalized
        for title in (
            "combat options",
            "rest variants",
            "gritty realism",
            "epic heroism",
            "customization options",
        )
    ):
        return "optional"
    if re.search(
        r"(?:^|\n)\s*(?:#+\s*)?(?:optional|variant)\s+(?:rule|rules)\b",
        str(chunk.get("text") or "")[:600],
        re.I,
    ):
        return "optional"
    return "core"


def _rule_reference_match(chunk: dict[str, Any], references: list[str]) -> int:
    headings = [str(chunk.get("section") or "")]
    headings.extend(re.split(r"\s*[>›]\s*", str(chunk.get("heading_path") or "")))
    headings.append(str(chunk.get("text") or "").split("\n", 1)[0].strip("# *_"))
    if any(reference_matches(h, ref) for h in headings for ref in references):
        return 2
    text = " " + normalize_reference(str(chunk.get("text") or "")) + " "
    return int(any(" " + normalize_reference(ref) + " " in text for ref in references))


def _edition_filter(rules_edition: str | None) -> Filter | None:
    if rules_edition not in ("2014", "2024"):
        return None
    return Filter(
        must_not=[
            FieldCondition(
                key="rules_edition",
                match=MatchValue(value="2024" if rules_edition == "2014" else "2014"),
            )
        ]
    )


def _scroll_rule_candidates(q: Any, collection: str, query_filter: Filter, *, limit: int) -> list:
    # Version pinning spans the whole read, including future pagination if the bound changes.
    from db.vector.versions import active_index_read

    with active_index_read(q, collection, query_filter) as active_filter:
        points, _offset = q.scroll(
            collection_name=collection,
            scroll_filter=active_filter,
            limit=limit,
            with_payload=True,
            with_vectors=False,
        )
    return [
        {
            **_chunk_from_payload(p.payload or {}, kind="rules"),
            "collection": collection,
            "score": None,
        }
        for p in points
    ]


def _lookup_rule_candidates(q: Any, collection: str, refs: list[str], edition: str | None) -> list:
    if not hasattr(q, "scroll"):
        return []  # Older injected clients may expose vector search only.
    edition_filter = _edition_filter(edition)
    try:
        exact_filter = Filter(
            must=[edition_filter] if edition_filter else None,
            should=[
                FieldCondition(key=key, match=MatchValue(value=ref))
                for ref in refs
                for key in ("section", "heading_path")
            ],
        )
        candidates = _scroll_rule_candidates(q, collection, exact_filter, limit=16)
        # Exact metadata avoids a common name being drowned out by incidental mentions.
        if not any(_rule_reference_match(c, refs) == 2 for c in candidates):
            lexical_filter = Filter(
                must=[edition_filter] if edition_filter else None,
                should=[FieldCondition(key="text", match=MatchText(text=ref)) for ref in refs],
            )
            candidates.extend(_scroll_rule_candidates(q, collection, lexical_filter, limit=64))
        return [
            {**c, "retrieval_method": "lexical"}
            for c in candidates
            if _rule_reference_match(c, refs)
            and (not edition or c.get("rules_edition") in (None, "unspecified", edition))
        ]
    except Exception as exc:
        # A vector answer remains useful when an older server cannot perform text lookup.
        logger.warning(
            "retrieval: rule reference lookup failed for %s (%s)", collection, type(exc).__name__
        )
        return []


def _rule_neighbors(q: Any, seed: dict, *, include_optional: bool) -> list[dict]:
    index = seed.get("chunk_index")
    section = seed.get("heading_path") or seed.get("section")
    if not hasattr(q, "scroll") or type(index) is not int or not section or not seed.get("source"):
        return []
    key = "heading_path" if seed.get("heading_path") else "section"
    # Legacy source names alone do not establish that adjacent indices share a version.
    lineage = ("source_sha256", "extraction_sha256")
    if not all(seed.get(k) for k in lineage):
        return []
    fields = ("source", *lineage, "rules_edition", "source_version", "index_version")
    expected = {k: seed[k] for k in fields if seed.get(k) is not None}
    expected[key] = section
    query_filter = Filter(
        must=[
            *(FieldCondition(key=k, match=MatchValue(value=v)) for k, v in expected.items()),
            FieldCondition(key="chunk_index", match=MatchAny(any=[index - 1, index + 1])),
        ]
    )
    try:
        neighbors = _scroll_rule_candidates(q, seed["collection"], query_filter, limit=2)
    except Exception as exc:
        logger.warning("retrieval: rule continuation lookup failed (%s)", type(exc).__name__)
        return []
    return [
        {**c, "rule_policy": rule_policy(c), "retrieval_method": "same_section_neighbor"}
        for c in neighbors
        if all(c.get(k) == v for k, v in expected.items())
        and all(c.get(k) == seed.get(k) for k in fields)
        and c.get("chunk_index") in (index - 1, index + 1)
        and (include_optional or rule_policy(c) == "core")
    ][:1]


def retrieve_active_scene(
    adventure_manifests: dict[str, dict],
    current_scene_id: str | None,
    world_flags: dict[str, str] | None = None,
    *,
    client: QdrantClient | None = None,
    rules_edition: str | None = None,
) -> dict:
    """Fetch exactly the reviewed current-scene spans, never broad semantic module context.

    All bindings need a valid manifest. Exactly one bound manifest must own this scene;
    ambiguous IDs require reviewer correction instead of selecting a source by accident.
    A missing/mutated indexed span fails closed even if other scenes or sources are present.
    """
    manifests = {name: require_published(value) for name, value in adventure_manifests.items()}
    owners = [
        (name, m)
        for name, m in manifests.items()
        if any(s.scene_id == current_scene_id for s in m.scenes)
    ]
    if len(owners) != 1:
        raise ManifestError("Current scene must belong to exactly one reviewed adventure manifest.")
    collection, manifest = owners[0]
    if rules_edition and manifest.rules_edition not in ("unspecified", rules_edition):
        raise ManifestError("The adventure edition does not match the campaign rules edition.")
    selection = select_scene_context(manifest, current_scene_id, world_flags)
    wanted = selection["player_passage_ids"] + selection["planner_passage_ids"]
    if not wanted:
        raise ManifestError("Current scene has no unlocked reviewed context.")
    q = _client(client)
    filters = {
        "source_sha256": manifest.source_sha256,
        "extraction_sha256": manifest.extraction_sha256,
        "rules_edition": manifest.rules_edition,
        "manifest_sha256": manifest.reviewed_sha256,
    }
    query_filter = Filter(
        must=[
            *(FieldCondition(key=k, match=MatchValue(value=v)) for k, v in filters.items()),
            FieldCondition(key="passage_id", match=MatchAny(any=wanted)),
        ]
    )
    expected = {p.passage_id: p for p in manifest.passages if p.passage_id in wanted}
    found = {}
    offset = None
    try:
        with active_index_read(q, collection, query_filter) as query_filter:
            while True:
                points, offset = q.scroll(
                    collection_name=collection,
                    scroll_filter=query_filter,
                    limit=256,
                    offset=offset,
                    with_payload=True,
                    with_vectors=False,
                )
                for point in points:
                    payload = point.payload or {}
                    passage_id = payload.get("passage_id")
                    passage = expected.get(passage_id)
                    if passage is None or passage_id in found:
                        raise ManifestError("Index contains ambiguous reviewed passage identities.")
                    expected_payload = {
                        **filters,
                        "source": manifest.source,
                        "page": passage.page,
                        "page_end": passage.page,
                        "source_pages": [passage.page],
                        "text_sha256": passage.text_sha256,
                        "audience": passage.audience,
                        "scene_ids": passage.scene_ids,
                        "entity_ids": passage.entity_ids,
                        "requires": passage.requires,
                        "span_start": passage.start,
                        "span_end": passage.end,
                    }
                    if (
                        any(payload.get(k) != v for k, v in expected_payload.items())
                        or not isinstance(payload.get("text"), str)
                        or text_digest(payload["text"]) != passage.text_sha256
                    ):
                        raise ManifestError("Indexed passage differs from its reviewed manifest.")
                    found[passage_id] = {
                        **_chunk_from_payload(payload, kind="adventure"),
                        "collection": collection,
                        "score": None,
                    }
                if offset is None:
                    break
    except ManifestError:
        raise
    except Exception as exc:
        raise RetrievalError(f"Reviewed scene retrieval failed for {collection}") from exc
    if set(found) != set(wanted):
        raise ManifestError("Reviewed scene passages are missing or stale in the adventure index.")
    return {
        "adventure_context": [found[pid] for pid in selection["player_passage_ids"]],
        "planner_context": [found[pid] for pid in selection["planner_passage_ids"]],
        "allowed_scene_ids": selection["allowed_scene_ids"],
        "discovery_entities": selection["entities"],
    }


def search_adventure_context(
    query_text: str,
    adventure_collections: list[str] | None,
    *,
    campaign_id: int | None = None,
    limit_per_collection: int = 5,
    client: QdrantClient | None = None,
    limit: int | None = DEFAULT_ADVENTURE_LIMIT,
    min_score: float | None = DEFAULT_MIN_SCORE,
    require_bound_sources: bool = False,
) -> list[dict[str, Any]]:
    """Bound modules plus optional campaign lore, best first.

    Module play uses `require_bound_sources` so another module or lore cannot hide a failed
    binding. Rules retrieval and optional lore keep the existing tolerant behavior.
    """
    bound = list(effective_adventure_collection_names(adventure_collections))
    names = list(bound)
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
        required_collections=bound if require_bound_sources else None,
    )


def retrieve_monster(
    name: str,
    *,
    client: QdrantClient | None = None,
    limit: int = DEFAULT_MONSTER_LIMIT,
    min_score: float | None = 0.15,
    collections: list[str] | None = None,
    rules_edition: str | None = None,
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

    vector = embed_query(
        f"{name.strip()} stat block: Armor Class, Hit Points, Speed, Challenge, Actions"
    )
    hits = _gather_hits(
        q, names, vector, max(10, limit * 5), kind="rules", rules_edition=rules_edition
    )
    if rules_edition:
        hits = [c for c in hits if c.get("rules_edition") in (None, "unspecified", rules_edition)]
    if min_score is not None:
        hits = [h for h in hits if h.get("score") is None or h["score"] >= min_score]

    for h in hits:
        h["rank_score"] = (h.get("score") or 0.0) + _monster_bonus(h, clean)
    hits.sort(key=lambda h: h["rank_score"], reverse=True)
    return _rank(hits, limit=limit, min_score=None, dedupe=True, presorted=True)


# --- internals -------------------------------------------------------------------------------


def _existing_only(q: Any, collection_names: list[str], *, refresh: bool = False) -> list[str]:
    """Drop duplicates and collections that do not exist (when the list can be fetched)."""
    names = list(dict.fromkeys(collection_names))
    existing = (
        existing_collection_names(q, max_age=0.0) if refresh else existing_collection_names(q)
    )
    if existing is None:
        return names
    present = [n for n in names if n in existing]
    skipped = [n for n in names if n not in existing]
    if skipped:
        logger.debug("retrieval: skipping missing collections %s", skipped)
    return present


def _gather_hits(
    q: Any,
    names: list[str],
    vector: list[float],
    limit_per_collection: int,
    *,
    kind: str,
    required_collections: list[str] | None = None,
    rules_edition: str | None = None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    failures = 0
    last_error: Exception | None = None
    for collection_name in names:
        try:
            edition_filter = _edition_filter(rules_edition)
            results = _query_collection(
                q, collection_name, vector, limit_per_collection, edition_filter
            )
        except Exception as e:
            if collection_name in (required_collections or []):
                raise RetrievalError(
                    f"required adventure collection query failed: {collection_name}"
                ) from e
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
        hits = sorted(
            hits,
            key=lambda h: h.get("score") if h.get("score") is not None else float("-inf"),
            reverse=True,
        )

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
    return normalize_reference(text)


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
    if chunk.get("stat_block") or (
        "Armor Class" in raw and ("Hit Points" in raw or "Challenge" in raw)
    ):
        bonus += 0.05
    return bonus


def _query_collection(q: QdrantClient, collection_name: str, vector, limit: int, query_filter=None):
    with active_index_read(q, collection_name, query_filter) as active_query_filter:
        return _query_active_collection(q, collection_name, vector, limit, active_query_filter)


def _query_active_collection(
    q: QdrantClient, collection_name: str, vector, limit: int, query_filter=None
):
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
            **({"query_filter": query_filter} if query_filter is not None else {}),
        )
        return response.points
    return q.search(
        collection_name=collection_name,
        query_vector=vector,
        limit=limit,
        with_payload=True,
        **({"query_filter": query_filter} if query_filter is not None else {}),
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
