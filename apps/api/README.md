# API

FastAPI application: routers (`campaigns`, `sessions`, `characters`, `seed`, `health`), schemas, services, and the React chat UI served under `/ui` when `frontend/dist` is built. The agent is imported in-process from `apps/agent`; there is no separate agent service.

## Chat UI

See [`frontend/README.md`](../../frontend/README.md). Build with `make frontend-build` (or `cd frontend && npm run build`); the Docker API image runs `npm run build` and copies `frontend/dist` into the container. Open `http://localhost:8000/ui/` after `make up`. The UI calls `POST /api/seed` from the home screen for a default user, campaign, character, and session, then streams turns via SSE on `/play/:sessionId`.

## Turn endpoints

Two endpoints share the same lock and persistence path; they differ in transport.

`POST /api/sessions/{session_id}/turn`. One JSON response after the agent finishes. Use this for `curl`, scripts, and tests. Errors are JSON `{code, message, detail}` with a generic, player-safe message; the real exception is only logged server-side. Status codes: `404` if the session is missing, `409` if another turn holds the lock (or `character_not_assigned` / the campaign has ended), `503` `llm_unavailable` when an agent call fails, `500` `turn_save_failed` / `turn_failed` otherwise.

`POST /api/sessions/{session_id}/turn/stream`. Server-Sent Events. Used by the UI. Frame types are `status` (stages: `parsing`, `retrieving`, `adjudicating`, `narrating`, `saving`), `token` (narration chunks; concatenated they form the full narration), `adjudication` (the structured `AdjudicationResult` once), `done` (`{narration, character, current_scene_id, combat_state, campaign_complete}`), and `error` (`{code, message, detail}`). Lock contention (409) is returned as a normal JSON error before the stream opens; failures after streaming begins arrive as an `error` event and the connection then closes.

```
curl -N -X POST "http://localhost:8000/api/sessions/1/turn/stream" \
  -H 'content-type: application/json' \
  -d '{"message":"I look around"}'
```

## Turn lifecycle

`acquire_turn_lock` takes the Redis key `turn:{session_id}` first, with a random owner token and a 30s TTL. A background task renews it every 10s for up to 300s, and release and renew only act if the key still holds this turn's token. Only then does `session.py::_load_turn_initial_state` read the session, campaign, character, prior messages, the rolling `memory_summary`, world flags, NPCs and any active combat into the agent's `initial_state`. The request's DB connection is released before the LLM calls.

The agent runs; `turn_runner.persist_turn` then opens one short-lived DB session and writes everything in a single commit: adjudication side effects (`apply_adjudication`), combat state, telemetry, the narration `event_log` row and campaign completion. Any save failure rolls back and surfaces as an error (an `error` SSE event when streaming). After the commit, a background task may fold older turns into a stored `memory_summary` event; its failures are logged, never raised.

`GET /api/sessions/{id}/combat` returns the active encounter in the same public shape as `done.combat_state` (roster HP and AC, initiative order, whose turn it is), or `null`, so the UI can restore its combat HUD after a reload. Setting `EDGAR_DEBUG=1` enables the per-stage `debug` frames that the UI's Developer mode shows.

`current_user_id()` looks up the seeded `dm@edgar.local` user; this is the single-user seam for solo play and is replaced by real auth when there is more than one user.

## Supported rules scope

`GET /api/rules/supported-scope` is read-only and static. It returns the code-owned contract
(`agent.supported_rules`): the mechanics the engine executes, the rules it does not execute, and
the solo-play adaptations. A declared action that falls in the unsupported list receives a narrative
ruling. Its narration and adjudication cannot state an engine effect, so spells outside the catalog,
maneuvers, advantage without an engine source, positioning and similar claims never change HP,
conditions or flags. The exceptions are engine facts: a catalog spell (`agent.spells`) is executed
by the engine, its dice, damage and healing come from the roll, and the spell-slot count is
persisted in the assignment's `stats["spell_slots_expended"]`; a combat maneuver the engine actually
applied (a won grapple, restraint or shove prone) is recorded in the combatant's `effects` and is
not stripped from the narration. The scope is also summarised in the rules and narrator prompts.

## Health

`/health` returns 200 without touching the DB (cheap; for compose healthchecks). `/ready` returns 200 only when Postgres, Redis and Qdrant all answer within a short timeout, 503 otherwise (for orchestrators that need a real readiness signal).

## Schemas

`schemas/` mirrors the SQLAlchemy ORM. `Create`/`Update` are request bodies; `Read` is the response body with `from_attributes=True` so ORM rows serialize directly.

### Campaign source availability and recovery

Apply Alembic revision `12_campaign_source_mode` before serving this version. It adds
`campaigns.source_mode` with default `module`; existing bindings, campaigns and saved play
remain intact. Generic narration now requires an explicit `homebrew` choice.

Campaign list/detail/selection responses include `source_mode`, `sources`,
`retrieval_degraded` and `play_allowed`. Each bound source reports its slug/title, availability
(`ready`, `missing`, `empty`, `partial`, `unavailable`, `processing` or `failed`), usable and
expected chunk counts, collection presence and `source_pdf_available`. Adventure responses
also report live availability, usable chunks and degradation. A metadata `ready` flag alone
is insufficient: readiness checks the live unnamed vector schema, vector presence and
nonblank text coverage, including the recorded chunk count when available. These checks do
not certify extraction quality or identify the original PDF's contents.

Creation and binding changes require usable sources in module mode. Opening an existing
campaign returns its recovery state even if its binding is orphaned. New sessions, intro,
sync turns and SSE turns check sources before play. A blocked request returns HTTP 409 with
`detail: {code: "campaign_sources_unavailable", message, retrieval_degraded: true, sources}`.
Source loss during a turn returns the same stable code and degradation (an SSE `error` frame
if the stream has opened), without saving that turn or inventing module narration. Successful
turns expose `source_mode`, `source_status` and `retrieval_degraded` without requiring debug.

`PATCH /campaigns/{id}` with `{"source_mode":"homebrew"}` records the player's explicit choice
and preserves the original bindings. Homebrew uses saved world/story state and campaign lore;
it does not query the unavailable bound modules. Switch back with `source_mode: "module"`
only after all bound sources are ready. An explicit empty `adventure_collections: []` means
no bound module; creation then requires `source_mode: "homebrew"`. Omitted bindings in module
creation retain the legacy default, which is subject to the same readiness check.

Recover an unusable source with multipart `file` at
`POST /campaigns/{id}/sources/{slug}/upload` (202). The slug must already be bound to that
campaign; recovery ingests the supplied PDF into that exact slug, never a suffixed duplicate.
Ready sources cannot be overwritten through this recovery endpoint. Poll campaign detail
for processing/ready/failed status. `POST /adventures/{slug}/retry` can reuse a retained source
PDF for a missing, empty, partial or failed index (or retry an explicitly AI-generated source);
a missing uploaded PDF returns 410. Historical logs and metadata cannot recreate a lost PDF.
Recovery uses the existing ingestion pipeline and extraction readiness checks described below.

# Extraction readiness and local previews

PDF upload and generated manuscripts keep a local extraction bundle beside their adventure
metadata. The canonical Markdown and JSON come from the same selected text used for chunks.
The JSON includes page extraction methods, quality diagnostics, warnings, source identity,
explicit contributing page coverage and representative text/stat/scene candidates.

`GET /api/adventures/{slug}/extraction` exposes bounded examples and diagnostics;
`GET /api/adventures/{slug}/extraction/markdown` downloads the full literal Markdown.
The library and source recovery panel expose these previews. Scene candidates are unreviewed;
extraction checks do not establish a canonical opening or resolve OCR glyph/semantic ambiguity.
The inspected PDF preview action sends both `reviewed_source_sha256` and
`reviewed_extraction_sha256` to `POST /api/adventures/{slug}/retry`. It requires a current,
quality-passed bundle and indexes that exact selected text; stale or changed bundles are rejected
before vector writes. The ordinary retained-source retry remains available without those hashes.

PDF OCR requires the operating-system Tesseract executable in addition to Python dependencies.
The API and ingestion Docker images include it; local development must install it separately.

Poor extraction and missing source/chunk coverage fail before embeddings or vector writes.
New ready sources must also match the validated source/extraction hashes, expected chunk count
and exact contributing pages in the live index. Specific `failure_code` and readable errors
remain in existing adventure metadata; the source PDF/manuscript preview is retained for recovery.
Generated-source retries prune the previous source chunks only after the complete replacement
has embedded and upserted successfully. A failed embedding attempt retains the previous index
and marks the source failed with its manuscript available for inspection and retry.
Legacy sources without extraction evidence retain the live coverage checks introduced by EDG-001.
Atomic index promotion and concurrent retry isolation remain EDG-015 work.

This contract uses the existing adventure JSON directories for metadata and extraction bundles,
and optional `source_pages`, `source_sha256` and `extraction_sha256` fields in Qdrant chunk
payloads. It introduces no relational database migration or separate extraction store.
