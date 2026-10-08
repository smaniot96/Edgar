"""
Qdrant vector database client.

`get_qdrant_client()` returns a process-wide singleton: QdrantClient holds an HTTP connection
pool, so building one per retrieval call (twice per turn) wasted a TCP/TLS handshake each time.
The client is safe to share across threads (it is used from `asyncio.to_thread` workers).
Call `get_qdrant_client.cache_clear()` to force a fresh client (e.g. after changing config).
"""
from functools import lru_cache

from qdrant_client import QdrantClient

from edgar_core.config import VECTOR_DB_API_KEY, VECTOR_DB_URL


@lru_cache(maxsize=1)
def get_qdrant_client() -> QdrantClient:
    """Return the shared QdrantClient connected to the configured Qdrant instance."""
    kwargs: dict = {"url": VECTOR_DB_URL}
    if VECTOR_DB_API_KEY:
        kwargs["api_key"] = VECTOR_DB_API_KEY
    return QdrantClient(**kwargs)
