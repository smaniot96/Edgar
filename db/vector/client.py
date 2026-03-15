"""
Qdrant vector database client.

Provides a factory function to create QdrantClient instances
using configuration from the project's .env / config.py.
"""
import sys
from pathlib import Path

# Ensure project root on path so config is importable
_root = Path(__file__).resolve().parent.parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from qdrant_client import QdrantClient
from config import VECTOR_DB_URL, VECTOR_DB_API_KEY


def get_qdrant_client() -> QdrantClient:
    """Create and return a QdrantClient connected to the configured Qdrant instance."""
    kwargs: dict = {"url": VECTOR_DB_URL}
    if VECTOR_DB_API_KEY:
        kwargs["api_key"] = VECTOR_DB_API_KEY
    return QdrantClient(**kwargs)