"""
OpenAI embedding helper for Qdrant vector storage.

Uses EMBEDDING_MODEL from config by default (1536 dimensions for text-embedding-3-small).
"""
from openai import APIError, OpenAI, RateLimitError
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from edgar_core.config import EMBEDDING_MODEL, OPENAI_API_KEY

# Vector size for text-embedding-3-small
VECTOR_SIZE = 1536


@retry(
    retry=retry_if_exception_type((RateLimitError, APIError)),
    wait=wait_exponential(min=1, max=30),
    stop=stop_after_attempt(5),
    reraise=True,
)
def _create_embeddings(client: OpenAI, texts: list[str], model: str):
    return client.embeddings.create(input=texts, model=model)


def get_embeddings(
    texts: list[str],
    model: str | None = None,
) -> list[list[float]]:
    """
    Generate embeddings for a list of text strings.

    Args:
        texts: List of strings to embed.
        model: OpenAI embedding model name (default: EMBEDDING_MODEL).

    Returns:
        List of embedding vectors (one per input text), preserving order.

    Raises:
        ValueError: If OPENAI_API_KEY is not set.
        openai.APIError: On API failures after retries.
    """
    if not OPENAI_API_KEY:
        raise ValueError(
            "OPENAI_API_KEY is not set. Add it to your .env file."
        )

    resolved_model = model or EMBEDDING_MODEL
    client = OpenAI(api_key=OPENAI_API_KEY)
    response = _create_embeddings(client, texts, resolved_model)
    return [item.embedding for item in response.data]
