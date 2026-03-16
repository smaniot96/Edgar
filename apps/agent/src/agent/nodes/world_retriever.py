"""WorldRetriever node: RAG via Qdrant + embeddings."""

from agent.state import AgentState


def world_retriever_node(state: AgentState) -> dict:
    """Fetch relevant rules and lore from Qdrant. TODO: Implement RAG."""
    return {"retrieved_context": []}
