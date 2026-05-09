"""OpenAI embedding helper.

`VECTOR_SIZE = 1536` matches `text-embedding-3-small`; if you switch model via the
`EMBEDDING_MODEL` env var, update collection creation in `apps/ingestion/src/ingestion/embed.py`
and recreate Qdrant collections, otherwise dimension mismatches will raise on upsert.

Retries transient OpenAI failures (rate limit, transient API error) up to 5 attempts with
exponential backoff so a single 429 does not kill a multi-thousand-page ingestion. Tests
typically monkeypatch `get_embeddings` so the retry path is rarely exercised in CI.
"""

from openai import APIError, OpenAI, RateLimitError
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from edgar_core.config import EMBEDDING_MODEL, OPENAI_API_KEY

VECTOR_SIZE = 1536


@retry(
    retry=retry_if_exception_type((RateLimitError, APIError)),
    wait=wait_exponential(min=1, max=30),
    stop=stop_after_attempt(5),
    reraise=True,
)
def _create_embeddings(client: OpenAI, texts: list[str], model: str):
    return client.embeddings.create(input=texts, model=model)


def get_embeddings(texts: list[str], model: str | None = None) -> list[list[float]]:
    """Order is preserved: `result[i]` is the embedding of `texts[i]`."""
    if not OPENAI_API_KEY:
        raise ValueError("OPENAI_API_KEY is not set. Add it to your .env file.")

    client = OpenAI(api_key=OPENAI_API_KEY)
    response = _create_embeddings(client, texts, model or EMBEDDING_MODEL)
    return [item.embedding for item in response.data]
