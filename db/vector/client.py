"""
Qdrant vector database client.

Provides a factory function to create QdrantClient instances
using configuration from the project's .env / edgar_core.config.
"""
from qdrant_client import QdrantClient

from edgar_core.config import VECTOR_DB_API_KEY, VECTOR_DB_URL


def get_qdrant_client() -> QdrantClient:
    """Create and return a QdrantClient connected to the configured Qdrant instance."""
    kwargs: dict = {"url": VECTOR_DB_URL}
    if VECTOR_DB_API_KEY:
        kwargs["api_key"] = VECTOR_DB_API_KEY
    return QdrantClient(**kwargs)
