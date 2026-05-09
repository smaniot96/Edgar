"""Rules-only RAG: shared db.vector helpers (core books, not adventure modules)."""

from db.vector import search_rules_context


def retrieve_rules_snippets(query: str, *, limit_per_collection: int = 5) -> list[dict]:
    """
    Return retrieved chunks from Player Handbook, DMG, and Monster Manual collections.

    Uses the same embedding + Qdrant wiring as the agent WorldRetriever rules bucket.
    """
    return search_rules_context(query, limit_per_collection=limit_per_collection)
