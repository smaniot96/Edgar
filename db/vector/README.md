# db.vector

Qdrant client, embeddings, and the rules vs adventure retrieval split.

`client.get_qdrant_client()` returns a process-wide singleton `QdrantClient` from `VECTOR_DB_URL` and optional `VECTOR_DB_API_KEY` (`get_qdrant_client.cache_clear()` to rebuild).

`embeddings.get_embeddings(texts, model=None)` calls OpenAI with `EMBEDDING_MODEL` (default `text-embedding-3-small`, 1536 dims). Wrapped in `tenacity.retry` against `RateLimitError | APIError`, exponential backoff 1 to 30 seconds, five attempts. The OpenAI client is shared. `embeddings.embed_query(text)` memoises single query embeddings in a thread-safe LRU (256 entries) so the rules and adventure searches of one turn embed the query once.

`collections` defines the canonical collection names (`rules_player_handbook`, `rules_dm_guide`, `rules_monster_manual`), legacy aliases, the default adventure module (`chalice_of_the_mountain_god`), the `campaign_lore_collection(campaign_id)` helper, `MONSTER_MANUAL_COLLECTIONS`, and `existing_collection_names(client)` (live collection list cached 30 s; `invalidate_collection_cache()` after creating one).

`retrieval.search_adventure_context` embeds a query once, searches existing candidate collections, ranks by vector score, drops hits below `min_score` (default 0.2), deduplicates text (word Jaccard ≥ 0.85), and caps the result (8 adventure). Automatic module play instead reads reviewed active-scene spans and keeps player/planner passages separate.

`retrieval.search_rules_context(query, references=["Second Wind"], rules_edition="2014")` combines vector candidates with exact heading lookup and a bounded text fallback. Exact references outrank incidental mentions; unrelated semantic hits cannot fill a focused rules query. Each book/reference lookup reads at most 16 exact-metadata candidates or another 64 text candidates, using the active-index filter. This bound prevents full-corpus scans; unusually common legacy names can need better heading metadata. A vector or embedding failure still permits an exact/lexical answer; a complete failure raises `RetrievalError`. Unknown editions remain explicitly unverified in citations and known incompatible editions are filtered before selection.

One adjacent chunk may continue a selected exact rule only when it shares the same section, source, extraction hashes, edition and available source/index versions. Neighbor expansion is bounded by the normal result cap (6 rules). Page proximity alone is insufficient; legacy chunks without source hashes do not expand.

Returned chunks preserve source/page/heading/hash/version fields and add `reference_match` (`exact`, `lexical`, `semantic`), `retrieval_method`, and `rule_policy` (`core`, `optional`, `variant`). Optional/variant headings and explicit legacy markers are excluded by default; an explicit inspection can request `include_optional=True` and gets labelled material. This heading-based legacy classification cannot identify unlabelled optional sidebars; such sources still need reviewed extraction metadata. Adjudicator/narrator contexts also reject marked optional/variant evidence, so inspection never silently enables those rules.

World retrieval plans independent named feature/spell, rest and check queries (at most four), with canonical English headings for the existing English corpus and English/Italian action aliases. Each focus contributes its leading evidence before continuations (at most six total). Casual conversation with no proposed check or named mechanic issues no rules query. NPC/scene names are excluded from core queries; current-scene manifests continue to own adventure scope. The parser can supply exact `entities.spell` / `entities.feature` names for topics beyond the small action-alias set.

`tests/agent/test_rule_relevance.py` freezes social, exploration, rest/Second Wind and Magic Missile actions against disposable in-memory Qdrant. It measures useful precision/focus coverage, then runs the actual adjudicator with a fake provider that verifies the supplied evidence and selects its reference IDs. Assertions cover cited rulings, physical pages, edition integrity and authoritative engine outcomes. These are deterministic contract evaluations, not a real-model fidelity or production-corpus recall claim. Both retrieval APIs accept an injected client; no live data or paid provider is required.


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
