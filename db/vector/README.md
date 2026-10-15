# db.vector

Qdrant client, embeddings, and the rules vs adventure retrieval split.

`client.get_qdrant_client()` returns a process-wide singleton `QdrantClient` from `VECTOR_DB_URL` and optional `VECTOR_DB_API_KEY` (`get_qdrant_client.cache_clear()` to rebuild).

`embeddings.get_embeddings(texts, model=None)` calls OpenAI with `EMBEDDING_MODEL` (default `text-embedding-3-small`, 1536 dims). Wrapped in `tenacity.retry` against `RateLimitError | APIError`, exponential backoff 1 to 30 seconds, five attempts. The OpenAI client is shared. `embeddings.embed_query(text)` memoises single query embeddings in a thread-safe LRU (256 entries) so the rules and adventure searches of one turn embed the query once.

`collections` defines the canonical collection names (`rules_player_handbook`, `rules_dm_guide`, `rules_monster_manual`), legacy aliases, the default adventure module (`chalice_of_the_mountain_god`), the `campaign_lore_collection(campaign_id)` helper, `MONSTER_MANUAL_COLLECTIONS`, and `existing_collection_names(client)` (live collection list cached 30 s; `invalidate_collection_cache()` after creating one).

`retrieval.search_rules_context` and `retrieval.search_adventure_context` embed a query once, search only the candidate collections that exist, then merge all hits, sort them globally by score, drop hits under `min_score` (default 0.2), de-duplicate exact / near-identical text (word Jaccard ≥ 0.85) and cap the total (`limit`: 6 rules, 8 adventure). Chunks are `{text, source, page, kind, collection, score}` plus `section` / `chapter` / `heading_path` / `page_end` / `stat_block` when the payload has them. A failing collection is logged as a warning (collection name + exception type); if every queried collection fails, `RetrievalError` is raised. Both accept an optional `client` so tests inject a fake Qdrant.

`retrieval.retrieve_monster(name)` searches only the Monster Manual collections and re-ranks hits so the stat block that names the creature comes first (default 2 chunks).

`versions.active_index_read(client, collection_name, query_filter=None)` is the required context
for all corpus queries, counts and paginated scrolls. It reads a single unembedded routing point
and yields the caller filter combined with exactly the active version of each managed source.
Legacy unmanaged sources stay visible. Keep the context open through the complete read so
promotion/pruning cannot invalidate an in-flight route. `source_status.inspect_source` verifies
only active coverage and excludes staged points and the routing point.

Version metadata stays in the existing Qdrant collection. Lock files use existing local data
storage (`EDGAR_INDEX_LOCK_DIR`); API, ingestion and rules-engine containers mount the same
`./data/.index-locks` directory. A host CLI must use that same directory. This protects concurrent
processes sharing one filesystem; it is not a multi-host distributed locking protocol. Each
source has an immutable content-derived `index_version`, a latest attempt token, and an active
receipt. Newer claims fence stale writers. Activation replaces one routing point only after
stored payload/vector validation; source-scoped obsolete-point cleanup follows under the lock.
