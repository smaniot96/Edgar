"""
OpenAI embedding helper for Qdrant vector storage.

Uses text-embedding-3-small by default (1536 dimensions).
"""
import sys
from pathlib import Path

# Ensure project root on path so config is importable
_root = Path(__file__).resolve().parent.parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from openai import OpenAI
from config import OPENAI_API_KEY

# Vector size for text-embedding-3-small
VECTOR_SIZE = 1536


def get_embeddings(
    texts: list[str],
    model: str = "text-embedding-3-small",
) -> list[list[float]]:
    """
    Generate embeddings for a list of text strings.

    Args:
        texts: List of strings to embed.
        model: OpenAI embedding model name.

    Returns:
        List of embedding vectors (one per input text), preserving order.

    Raises:
        ValueError: If OPENAI_API_KEY is not set.
        openai.APIError: On API failures.
    """
    if not OPENAI_API_KEY:
        raise ValueError(
            "OPENAI_API_KEY is not set. Add it to your .env file."
        )

    client = OpenAI(api_key=OPENAI_API_KEY)
    response = client.embeddings.create(input=texts, model=model)
    return [item.embedding for item in response.data]
