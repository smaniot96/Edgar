"""WorldRetriever node: RAG via Qdrant + embeddings."""

import sys
from pathlib import Path

# Ensure project root on path so db.vector can load config
_root = Path(__file__).resolve().parents[5]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from db.vector import get_embeddings, get_qdrant_client

from agent.state import AgentState


def world_retriever_node(state: AgentState) -> dict:
    """Fetch relevant rules and lore from Qdrant."""
    player_input = state.get("player_input")
    parsed_input = state.get("parsed_input")
    campaign_id = state.get("campaign_id")

    if not player_input:
        return {"retrieved_context": []}

    # Build query from player input and intent
    query_parts = [player_input]
    if parsed_input:
        query_parts.append(f"Intent: {parsed_input.intent}")
    query = " ".join(query_parts)

    try:
        client = get_qdrant_client()
        embeddings = get_embeddings([query])
        vector = embeddings[0]

        # Search collections (dm_guide, monster_manual, player_handbook)
        collections = ["player_handbook", "dm_guide", "monster_manual"]
        if campaign_id:
            collections.append(f"campaign_lore_{campaign_id}")

        all_results = []
        for collection in collections:
            try:
                results = client.search(
                    collection_name=collection,
                    query_vector=vector,
                    limit=5,
                    with_payload=True,
                )
                for r in results:
                    payload = r.payload or {}
                    all_results.append({
                        "text": payload.get("text", ""),
                        "source": payload.get("source", ""),
                        "page": payload.get("page"),
                    })
            except Exception:
                continue  # Collection may not exist

        return {"retrieved_context": all_results[:10]}
    except Exception as e:
        return {"retrieved_context": [], "error": str(e)}
