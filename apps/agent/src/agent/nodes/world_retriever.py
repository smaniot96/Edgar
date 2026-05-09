"""Retrieve rules (PHB/DMG/MM) and adventure-module chunks from Qdrant in parallel.

The two buckets are kept separate so the adjudicator can rule on rules excerpts only and the
narrator can ground story details in module text. `retrieved_context` is the merged view kept
for backwards compatibility with older nodes; new code should use `rules_context` and
`adventure_context` directly.

`qdrant-client` is sync, so each search is wrapped in `asyncio.to_thread` and the two run
concurrently via `asyncio.gather`. Tests pass a fake QdrantClient through `state["qdrant_client"]`.
"""

import asyncio

from db.vector import search_adventure_context, search_rules_context

from agent.state import AgentState

_LIMIT_PER_COLLECTION = 5
_MERGED_TOP_K = 20


async def world_retriever_node(state: AgentState) -> dict:
    player_input = state.get("player_input")
    parsed_input = state.get("parsed_input")
    campaign_id = state.get("campaign_id")
    adventure_collections = state.get("adventure_collections")

    if not player_input:
        return {"rules_context": [], "adventure_context": [], "retrieved_context": []}

    # Append the parsed intent to the query so retrieval reflects "I attack" vs "I greet".
    query_parts = [player_input]
    if parsed_input:
        query_parts.append(f"Intent: {parsed_input.intent}")
    query = " ".join(query_parts)

    client = state.get("qdrant_client")

    try:
        rules_context, adventure_context = await asyncio.gather(
            asyncio.to_thread(
                search_rules_context, query, limit_per_collection=_LIMIT_PER_COLLECTION, client=client
            ),
            asyncio.to_thread(
                search_adventure_context,
                query,
                adventure_collections,
                campaign_id=campaign_id,
                limit_per_collection=_LIMIT_PER_COLLECTION,
                client=client,
            ),
        )
    except Exception as e:
        # Surface as `error` so the API returns 503; an unreachable Qdrant means no RAG and
        # adjudication would otherwise rule on no rules context, which is worse than failing fast.
        return {
            "rules_context": [],
            "adventure_context": [],
            "retrieved_context": [],
            "error": str(e),
        }

    return {
        "rules_context": rules_context,
        "adventure_context": adventure_context,
        "retrieved_context": (rules_context + adventure_context)[:_MERGED_TOP_K],
    }
