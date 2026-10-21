# Active campaign preparation: graph v2

The only campaign creation executor is `apps/api/services/campaign_prep/graph.py`.
It runs a LangGraph StateGraph with source acquisition, campaign spine, batched chapter
bundles, explicit continuity joins, deterministic assembly, independent review, scoped
repair and publication. PDF upload and AI creation both enter `runtime.prepare_adventure`.
No hero is read during preparation. Select a hero when creating play afterward.

## Artifacts and authority

`core/src/edgar_core/campaign_semantics.py` defines reusable narrative records, one
pacing goal, source/adaptation/authored origins, visible entities and earned clue reveals.
`Store` owns graph state, artifacts, dependency hashes, task dispatches, provider outputs,
review fingerprints, findings, usage and cancellation in each campaign's
`preparation-v2/graph.sqlite3`. LangGraph has no independent durable scheduler.
The frozen request contains the rules edition, source identity and preferences, never a
character sheet. Existing campaign files and old preparation checkpoints are not migrated.

PDF acquisition renders every physical page at 200 dpi and runs local Tesseract, including
pages with a native text layer. It retains the original upload, raster, raw OCR, normalized
text, TSV words/confidence/reading order, hOCR, block regions, quality diagnostics and
hashes. The author sees OCR; native PDF text is never an authoring fallback. Sparse or
uncertain pages, plus useful map/table pages requested by the spine, receive targeted
Luna visual interpretation. OCR is not represented as verified mechanical authority.

AI acquisition starts directly from the premise, theme and requested scope. It does not
create a PDF, import old outlines or invoke the deleted generation code. Both inputs share
stable IDs, future truths, batched chapter authoring and the remaining graph.

Every semantic request uses `gpt-6-luna` at `medium`, including critics, identity joins,
source reading, visual interpretation and repairs. Prompts are versioned reviewable files
under `apps/api/services/campaign_prep/prompts/`. Operational concurrency is bounded;
token packets shape context and latency without truncating evidence. The USD 5 target is
advisory. No dollar, call, repair-round or overall-duration stopping cap is imposed.

Dispatch identity is durable before submission and complete provider output is durable
before parsing. Background Responses jobs recover by known ID; ambiguous submissions
hold for explicit response-ID reconciliation rather than blind payment. Cancellation is
persistent, including cancellation retries for known jobs after application restart.
Embedding submissions/vectors use the same receipt owner and separate truthful accounting.

## Play and publication

`compile.py` uses existing engine manifests, verified creature templates and local assembly.
Encounter quantities remain reusable source/module quantities. Actual combat fitting,
checks, HP, inventory and committed state remain engine-owned. The engine receives one
objective representation for narrative goals and can adjudicate any credible creative
completion. Private DM evidence, visible player passages and earned clue reveals remain
separate. Authored record markers do not pretend to be PDF citations.

The final critic is independent of the author. Reviews certify record/dependency
fingerprints. Typed repairs change only affected records and their real dependency
closure; transitive fingerprinting preserves unrelated coverage. Unchanged repair results
hold with findings retained. Ready requires current coverage, no material findings, exact
compiled/guide files, a publication receipt, a published manifest and the active immutable
index version. Manual manifest writers cannot bypass this boundary for prepared sources.

## Removal and retained primitives

Removed executable services: `preparation.py`, `preparation_runtime.py`,
`preparation_stages.py`, `preparation_blueprint.py`, `preparation_authoring.py`,
`preparation_content.py`, `preparation_corrections.py`, `preparation_grounding.py`,
`preparation_proposals.py`, `preparation_provider.py`, `preparation_responses.py`,
`preparation_review_batches.py`, `preparation_sources.py`, `preparation_books.py`,
`preparation_commission.py`, `preparation_store.py`, `campaign_generator.py` and
`tools/compact_preparation.py`. Their inline prompts, P-stage selectors, hero-bound setup,
hard cost/route/asset flags and obsolete tests were removed. Core authoring contracts in
`preparation.py`, `coherence.py` and `grounding.py` were deleted. The independent CR/XP
parser moved to `creature_xp.py`; public status DTOs moved to `preparation_status.py`.
Old asset planning/generation dispatch and stale recovery writers were deleted.

Retained infrastructure serves the new workflow independently: rulebook ingestion,
verified creature lookup/stat parsing, immutable index activation, manifest span checks,
engine/combat/character/play ownership, signed archived asset readers and player map
projection. `campaign_prep/archive.py` is a small inert reader for saved v1 data.
Startup only dispatches explicitly marked `campaign-graph-2.0` work. Archived Phandelver
is held and cannot be retried, reuploaded or automatically relabelled into the new graph.

See `campaign-preparation-dev-rollout.md` for committed-source Docker builds and the
original-stack data preservation procedure. Real demonstration receipts and data hashes
are kept outside Git; the delivery report records their outcomes and measured usage.
