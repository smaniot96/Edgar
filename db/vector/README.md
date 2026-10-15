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


`retrieval.retrieve_monster(name)` returns only whole, exactly named Monster Manual stat records (default at most 2 records). It performs a paginated metadata lookup under the active index read lock; embeddings, substring bonuses, prose mentions and near-duplicate ranking cannot establish creature identity. It returns `[]` for an unresolved name, preserving the list API. `min_score` remains accepted for older callers but has no effect on exact lookup. Case and punctuation normalize through the shared `references` primitive; all words, numbers, dragon ages and colors remain significant. Plurals and arbitrary aliases require an explicit reviewed identity rather than guessing.

`creatures.creature_payload_fields(chunk)` stamps `creature_name` / `creature_key` only on complete, explicitly headed single stat records. Conflicting titles, missing AC/HP, and mixed stat blocks are rejected. `creatures.resolve_creature(...)` additionally requires a unique record, the selected explicit edition and verified source/extraction checksums, and raises `CreatureResolutionError` with recovery guidance when identity is unresolved. Optional collection/source constraints bind reviewed templates to the intended source. Legacy chunks without normalized fields can use an exact section spelling; legacy mixed or unverified data needs reviewed reingestion. Whole record text and provenance survive retrieval without a 500-character prefix.

Named module actors use the existing reviewed manifest's optional `entities[].stat_reference`, available only in the unlocked active scene. For a template, set `{"kind":"template","creature_name":"Bandit","collection":"rules_monster_manual","source_sha256":"<64-character checksum>","rules_edition":"2014"}`. For module-specific statistics, set `{"kind":"module","creature_name":"Captain Ash","passage_id":"captain_stats"}` and bind that exact complete passage to the actor. A module reference cannot point to excluded/unreviewed material or bypass scene/prerequisite gates. Index passage checksums and the manifest review receipt still apply. Adding or changing a reference requires reviewing and publishing that manifest; absent references preserve receipts issued before this extension.

New encounters reject unknown identities and model-proposed renames/additions with the public `creature_identity_unresolved` error (HTTP 409). The builder receives each complete record. Exact foes take their numbers, attacks and traits from the record itself (`agent.monster_stats`, EDG-004); the builder's numbers are ignored and an exact record is never rescaled by the solo budget. Records whose stat block cannot be read exactly block the encounter. Combatants persist a compact private `source_reference` and `creature_id`, while canonical full text remains in its index. Player and debug responses omit these private references. Existing saved encounters retain their numeric state and need no retroactive identity lookup.

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
