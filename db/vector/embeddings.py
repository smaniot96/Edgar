"""OpenAI embedding helper.

`VECTOR_SIZE = 1536` matches `text-embedding-3-small`; if you switch model via the
`EMBEDDING_MODEL` env var, update collection creation in `apps/ingestion/src/ingestion/embed.py`
and recreate Qdrant collections, otherwise dimension mismatches will raise on upsert.

Retries transient OpenAI failures (rate limit, transient API error) up to 5 attempts with
exponential backoff so a single 429 does not kill a multi-thousand-page ingestion. Tests
typically monkeypatch `get_embeddings` so the retry path is rarely exercised in CI.

`embed_query` adds a small thread-safe LRU cache on top for single query strings: a turn
searches the rules and adventure buckets concurrently with the same query, and that query used
to be embedded twice. Concurrent misses for the same key wait for the first caller instead of
issuing a duplicate request.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from functools import lru_cache

from openai import APIError, OpenAI, RateLimitError
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from edgar_core.config import EMBEDDING_MODEL, OPENAI_API_KEY

VECTOR_SIZE = 1536

QUERY_CACHE_SIZE = 256


@retry(
    retry=retry_if_exception_type((RateLimitError, APIError)),
    wait=wait_exponential(min=1, max=30),
    stop=stop_after_attempt(5),
    reraise=True,
)
def _create_embeddings(client: OpenAI, texts: list[str], model: str):
    return client.embeddings.create(input=texts, model=model)


@lru_cache(maxsize=1)
def _openai_client(api_key: str) -> OpenAI:
    """Shared OpenAI client (connection pool reuse); keyed by api key so tests can swap it."""
    return OpenAI(api_key=api_key)


def get_embeddings(texts: list[str], model: str | None = None) -> list[list[float]]:
    """Order is preserved: `result[i]` is the embedding of `texts[i]`."""
    if not OPENAI_API_KEY:
        raise ValueError("OPENAI_API_KEY is not set. Add it to your .env file.")

    client = _openai_client(OPENAI_API_KEY)
    response = _create_embeddings(client, texts, model or EMBEDDING_MODEL)
    return [item.embedding for item in response.data]


_query_cache: OrderedDict[tuple[str, str], tuple[float, ...]] = OrderedDict()
_query_cache_lock = threading.Lock()
_inflight: dict[tuple[str, str], threading.Lock] = {}


def embed_query(text: str, model: str | None = None) -> list[float]:
    """Embed one query string, memoised in a process-wide LRU (size `QUERY_CACHE_SIZE`)."""
    key = (model or EMBEDDING_MODEL, text)
    with _query_cache_lock:
        cached = _query_cache.get(key)
        if cached is not None:
            _query_cache.move_to_end(key)
            return list(cached)
        key_lock = _inflight.setdefault(key, threading.Lock())

    with key_lock:
        with _query_cache_lock:
            cached = _query_cache.get(key)
            if cached is not None:
                _query_cache.move_to_end(key)
                return list(cached)
        try:
            vector = get_embeddings([text], model=model)[0]
        except BaseException:
            with _query_cache_lock:
                _inflight.pop(key, None)
            raise
        with _query_cache_lock:
            _query_cache[key] = tuple(vector)
            _query_cache.move_to_end(key)
            _inflight.pop(key, None)
            while len(_query_cache) > QUERY_CACHE_SIZE:
                _query_cache.popitem(last=False)
    return list(vector)


def clear_query_embedding_cache() -> None:
    """Drop all memoised query embeddings (tests, or after switching EMBEDDING_MODEL)."""
    with _query_cache_lock:
        _query_cache.clear()
