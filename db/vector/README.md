# db.vector

Qdrant client, embeddings, and the rules vs adventure retrieval split.

`client.get_qdrant_client()` returns a configured `QdrantClient` from `VECTOR_DB_URL` and optional `VECTOR_DB_API_KEY`.

`embeddings.get_embeddings(texts, model=None)` calls OpenAI with `EMBEDDING_MODEL` (default `text-embedding-3-small`, 1536 dims). Wrapped in `tenacity.retry` against `RateLimitError | APIError`, exponential backoff 1 to 30 seconds, five attempts.

`collections` defines the canonical collection names (`rules_player_handbook`, `rules_dm_guide`, `rules_monster_manual`), legacy aliases, the default adventure module (`chalice_of_the_mountain_god`), and the `campaign_lore_collection(campaign_id)` helper.

`retrieval.search_rules_context` and `retrieval.search_adventure_context` embed a query once and search the relevant collections, returning a list of `{text, source, page, kind, collection}`. Missing collections are skipped silently. Both accept an optional `client` so tests inject a fake Qdrant.
