# Reviewing adventure manifests

The canonical extraction bundle remains the source text. A manifest stores exact physical
page/start/end spans and checksums, canonical scene and entity IDs, branch links, discovery
flags, and explicit player/planner/excluded classifications. It does not copy the manuscript
or store campaign progress. Session scene IDs and existing WorldFlag rows remain authoritative.

After extraction/index readiness, use the local API review interface:

1. `POST /api/adventures/{slug}/manifest/compile` creates an **unreviewed** draft. Page
   boundaries are suggestions; merge/rename them into canonical scenes as needed.
2. `GET /api/adventures/{slug}/manifest` returns the editable manifest, exact source passage
   text and a `reviewed_digest`. Inspect all passages against the retained PDF/manuscript.
3. `PUT /api/adventures/{slug}/manifest` saves the edited manifest. Every nonwhitespace source
   character must belong to exactly one span. Split passages that mix player and DM material;
   mark irrelevant front matter `excluded`. Changed spans receive source-derived checksums
   in the returned inspection response. No edit automatically approves the draft.
4. Set an explicit `opening_scene_id` with unconditional player-safe text. Add canonical
   actor/clue/hazard/ending entities and `entity_ids` on their passages. Preserve alternative
   branches in each scene's `links`; link prerequisites are exact WorldFlag key/value matches.
   A clue's `discovery_flag` uses value `discovered`. Attach only relevant cross-references
   using `references` passage IDs, and gate those passages through `requires` where necessary.
5. `POST /api/adventures/{slug}/manifest/publish` with the exact last-inspected
   `{"reviewed_digest":"…","reviewer":"…"}` validates the source and receipt, indexes the
   reviewed spans, and publishes the manifest in existing adventure metadata only on success.

Publication is an explicit review acknowledgment, not reviewer authentication. A failed
indexing run stays failed and retains its draft. Replacing the PDF, editing its selected
extraction, changing the declared edition, or changing the manifest invalidates prior review.
Generated manuscripts use the same preserved extraction bundle and review flow without a PDF.

Runtime retrieval loads the current scene, unlocked prerequisites and explicit references;
links do not load neighboring room text. Player context excludes planner/excluded spans.
Canonical discoveries persist through the existing structured adjudication/world-flag writer;
only reviewed branch destinations are accepted. Unreviewed legacy sources require this review
flow for module play, or an explicit homebrew choice. Broad semantic retrieval remains available
as a low-level inspection helper and does not certify player-safe content.


A fresh session takes its opening from the **first** campaign binding's explicit
`opening_scene_id`. Reordering bindings changes that choice; secondary sources never
substitute their openings. Front matter and DM synopses must be excluded or planner-only.
The intro consumes complete reviewed player spans, without the former seven-chunk or
700-character clipping. Missing/stale review gives `adventure_manifest_review_required`;
source outages retain `campaign_sources_unavailable`. Only explicit homebrew permits a
generic opening. Scene and opening narration commit together in the caller's transaction;
existing narration remains idempotent even after source changes. If opening generation
fails, the first turn can still start at the reviewed opening.

## Source editions and references

Campaign `rules_edition` explicitly selects `2014` (the existing engine policy) or `2024`.
This pins retrieval evidence; it does not implement a second numeric rules engine. Uploads,
source metadata and CLI `--rules-edition` declare `2014`, `2024` or `unspecified`. Existing
source editions stay unspecified; filenames never infer them. A metadata edition correction
requires fresh manifest review/indexing; a known conflicting extraction declaration must
also be corrected and reviewed. A known conflicting edition is excluded before rule-result
limits/deduplication, or blocks an incompatible active module. Unknown editions remain
visible as unverified evidence.

Stable reference IDs cover source/extraction identity, physical pages and passage identity.
Prompts retain those references. The adjudicator proposes only IDs from supplied evidence;
invented/conflicting IDs are dropped. Existing adjudication events retain exact references
for private diagnostics. Narration events, sync/SSE responses and `/api/sessions/{id}/messages`
JSON (reload/export) retain public references and `ruling_citation_ids` to distinguish evidence
explicitly selected for the ruling from context supplied to narration. Public references
contain neither excerpts nor private scene/passage identifiers. Source/version/page details
remain available in the transcript. Tests cover real synthetic PDFs, disposable Postgres and
in-memory Qdrant; they verify selection/prompt boundaries, not a real-model leakage rate.
