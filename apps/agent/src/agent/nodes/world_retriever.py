"""Retrieve rules (PHB/DMG/MM) and adventure-module chunks from Qdrant in parallel.

The two buckets are kept separate so the adjudicator can rule on rules excerpts only and the
narrator can ground story details in module text. `retrieved_context` is the merged view kept
for backwards compatibility with older nodes; new code should use `rules_context` and
`adventure_context` directly.

The query is the parser's `retrieval_query` (enriched with scene/target/rule) when present,
else the raw player input. Both buckets get the *identical* string, so the memoised
`db.vector.embed_query` embeds it once per turn.

`qdrant-client` is sync, so each search is wrapped in `asyncio.to_thread` and the two run
concurrently via `asyncio.gather`. Tests pass a fake QdrantClient through `state["qdrant_client"]`.

Retrieval failures degrade instead of failing the turn: the bucket that failed comes back
empty and `retrieval_degraded=True` is set so the UI/debug panel can warn.
"""

import asyncio
import logging

from agent.state import AgentState
from db.vector import search_adventure_context, search_rules_context
from db.vector.collections import effective_adventure_collection_names
from db.vector.source_status import inspect_source

log = logging.getLogger(__name__)

_LIMIT_PER_COLLECTION = 5
_MERGED_TOP_K = 20
_MAX_QUERY_LEN = 300


def build_retrieval_query(state: AgentState) -> str:
    player_input = state.get("player_input") or ""
    parsed = state.get("parsed_input")
    query = (getattr(parsed, "retrieval_query", None) or "").strip()
    if not query:
        query = player_input
        if parsed is not None and getattr(parsed, "target", None):
            query = f"{query} {parsed.target}"
    return query[:_MAX_QUERY_LEN]


async def world_retriever_node(state: AgentState) -> dict:
    player_input = state.get("player_input")
    campaign_id = state.get("campaign_id")
    adventure_collections = [] if state.get("source_mode") == "homebrew" else state.get("adventure_collections")

    if not player_input:
        return {"rules_context": [], "adventure_context": [], "retrieved_context": []}

    query = build_retrieval_query(state)
    client = state.get("qdrant_client")

    previous_sources = {s["slug"]: s for s in state.get("source_status", [])}
    expected = {slug: source.get("expected_chunks") for slug, source in previous_sources.items()}
    names = (
        []
        if state.get("source_mode") == "homebrew"
        else effective_adventure_collection_names(adventure_collections)
    )
    statuses = await asyncio.to_thread(
        lambda: [inspect_source(n, expected_chunks=expected.get(n), client=client) for n in names]
    )
    statuses = [{**previous_sources.get(s["slug"], {}), **s} for s in statuses]
    source_degraded = any(s["status"] != "ready" for s in statuses)

    results = await asyncio.gather(
        asyncio.to_thread(
            search_rules_context, query, limit_per_collection=_LIMIT_PER_COLLECTION, client=client
        ),
        asyncio.to_thread(
            search_adventure_context,
            query,
            adventure_collections,
            campaign_id=campaign_id,
            require_bound_sources=state.get("source_mode") == "module",
            limit_per_collection=_LIMIT_PER_COLLECTION,
            client=client,
        ),
        return_exceptions=True,
    )

    degraded = source_degraded or bool(state.get("retrieval_degraded"))
    buckets: list[list] = []
    for name, res in zip(("rules", "adventure"), results, strict=True):
        if isinstance(res, BaseException):
            # Qdrant blip / embeddings outage: keep the turn playable with empty context.
            log.warning(
                "retrieval_degraded",
                extra={"bucket": name, "error": f"{type(res).__name__}: {res}"},
            )
            degraded = True
            if name == "adventure" and names:
                source_degraded = True
                statuses = [
                    {
                        **s,
                        "status": "unavailable",
                        "message": "Adventure retrieval failed. Try again or choose homebrew play.",
                    }
                    for s in statuses
                ]
            buckets.append([])
        else:
            buckets.append(list(res or []))
    rules_context, adventure_context = buckets

    out: dict = {
        "source_status": statuses or state.get("source_status", []),
        "retrieval_degraded": degraded,
        "rules_context": rules_context,
        "adventure_context": adventure_context,
        "retrieved_context": (rules_context + adventure_context)[:_MERGED_TOP_K],
    }
    if source_degraded and state.get("source_mode") == "module":
        out["error"] = "Adventure source availability changed during retrieval."
        out["error_code"] = "campaign_sources_unavailable"
    return out
