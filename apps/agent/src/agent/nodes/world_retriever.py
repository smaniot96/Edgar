"""WorldRetriever node: RAG via Qdrant — rules (PHB/DMG/MM) vs adventure modules."""

import asyncio

from db.vector import search_adventure_context, search_rules_context

from agent.state import AgentState


async def world_retriever_node(state: AgentState) -> dict:
    """Fetch rules excerpts and adventure-module lore from Qdrant (separate buckets)."""
    player_input = state.get("player_input")
    parsed_input = state.get("parsed_input")
    campaign_id = state.get("campaign_id")
    adventure_collections = state.get("adventure_collections")

    if not player_input:
        return {
            "rules_context": [],
            "adventure_context": [],
            "retrieved_context": [],
        }

    query_parts = [player_input]
    if parsed_input:
        query_parts.append(f"Intent: {parsed_input.intent}")
    query = " ".join(query_parts)

    client = state.get("qdrant_client")

    try:
        rules_context, adventure_context = await asyncio.gather(
            asyncio.to_thread(
                search_rules_context, query, limit_per_collection=5, client=client
            ),
            asyncio.to_thread(
                search_adventure_context,
                query,
                adventure_collections,
                campaign_id=campaign_id,
                limit_per_collection=5,
                client=client,
            ),
        )
        merged = (rules_context + adventure_context)[:20]
        return {
            "rules_context": rules_context,
            "adventure_context": adventure_context,
            "retrieved_context": merged,
        }
    except Exception as e:
        return {
            "rules_context": [],
            "adventure_context": [],
            "retrieved_context": [],
            "error": str(e),
        }
