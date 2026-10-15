# API

FastAPI application: routers (`campaigns`, `sessions`, `characters`, `seed`, `health`), schemas, services, and the React chat UI served under `/ui` when `frontend/dist` is built. The agent is imported in-process from `apps/agent`; there is no separate agent service.

## Chat UI

See [`frontend/README.md`](../../frontend/README.md). Build with `make frontend-build` (or `cd frontend && npm run build`); the Docker API image runs `npm run build` and copies `frontend/dist` into the container. Open `http://localhost:8000/ui/` after `make up`. The UI calls `POST /api/seed` from the home screen for a default user, campaign, character, and session, then streams turns via SSE on `/play/:sessionId`.

## Turn endpoints

Two endpoints share the same lock and persistence path; they differ in transport.

`POST /api/sessions/{session_id}/turn`. One JSON response after the agent finishes. Use this for `curl`, scripts, and tests. Errors are JSON `{code, message, detail}` with a generic, player-safe message; the real exception is only logged server-side. Status codes: `404` if the session is missing, `409` if another turn holds the lock (or `character_not_assigned` / the campaign has ended), `503` `llm_unavailable` when an agent call fails, `500` `turn_save_failed` / `turn_failed` otherwise.

`POST /api/sessions/{session_id}/turn/stream`. Server-Sent Events. Used by the UI. Frame types are `status` (stages: `parsing`, `retrieving`, `adjudicating`, `narrating`, `saving`), `token` (narration chunks; concatenated they form the full narration), `adjudication` (the structured `AdjudicationResult` once), `done` (`{narration, character, current_scene_id, combat_state, campaign_complete, roll_status, roll}`; `roll` is the turn's original ruling and dice, the same record `GET /api/sessions/{session_id}/messages` returns on reload, and null when no dice were rolled), and `error` (`{code, message, detail}`). Lock contention (409) is returned as a normal JSON error before the stream opens; failures after streaming begins arrive as an `error` event and the connection then closes.

```
curl -N -X POST "http://localhost:8000/api/sessions/1/turn/stream" \
  -H 'content-type: application/json' \
  -d '{"message":"I look around"}'
```

## Turn lifecycle

`acquire_turn_lock` takes the Redis key `turn:{session_id}` first, with a random owner token and a 30s TTL. A background task renews it every 10s for up to 300s, and release and renew only act if the key still holds this turn's token. Only then does `session.py::_load_turn_initial_state` read the session, campaign, character, prior messages, the rolling `memory_summary`, world flags, NPCs and any active combat into the agent's `initial_state`. The request's DB connection is released before the LLM calls.

The agent runs; `turn_runner.persist_turn` then opens one short-lived DB session and writes everything in a single commit: adjudication side effects (`apply_adjudication`), combat state, telemetry, the narration `event_log` row and campaign completion. Any save failure rolls back and surfaces as an error (an `error` SSE event when streaming). After the commit, a background task may fold older turns into a stored `memory_summary` event; its failures are logged, never raised.

`GET /api/sessions/{id}/combat` returns the active encounter in the same public shape as `done.combat_state` (roster HP and AC, initiative order, whose turn it is), or `null`, so the UI can restore its combat HUD after a reload. Setting `EDGAR_DEBUG=1` enables the per-stage `debug` frames that the UI's Developer mode shows. Each combat round also stores one `combat_round` event_log row in the turn transaction (actors, original dice, outcomes, dispositions, and only the stage timings the round measured; unmeasured stages are listed in `timings_unavailable`). With `debug` and `EDGAR_DEBUG=1` the `debug` frame and sync response carry it as `combat_diagnostics`; `GET /api/sessions/{id}/diagnostics/combat` returns stored rounds for reload (404 when `EDGAR_DEBUG` is off). A turn with no stored row was not captured; it is never reported as zero.

`current_user_id()` looks up the seeded `dm@edgar.local` user; this is the single-user seam for solo play and is replaced by real auth when there is more than one user.

## Supported rules scope

`GET /api/rules/supported-scope` is read-only and static. It returns the code-owned contract
(`agent.supported_rules`): the mechanics the engine executes, the rules it does not execute, and
the solo-play adaptations. A declared action that falls in the unsupported list receives a narrative
ruling. Its narration and adjudication cannot state an engine effect, so spells outside the catalog,
maneuvers, advantage without an engine source, positioning and similar claims never change HP,
conditions or flags. The exceptions are engine facts: a catalog spell (`agent.spells`) is executed
by the engine, its dice, damage and healing come from the roll, and the spell-slot count is
persisted in the assignment's `stats["spell_slots_expended"]`; spent Hit Dice are persisted in
`stats["hit_dice"]` (both are protected from manual sheet edits), and a rest turn stores its engine
evidence (the hit die rolled, HP regained, Hit Dice left) as `rest` in the roll record; the uses of
Action Surge and Rage are persisted per feature in `stats["class_features"]`, and a Rage flag lasts
only for its encounter (`raging` in the combat state); a combat maneuver the engine actually
applied (a won grapple, restraint or shove prone) and a spell condition it recorded (a foe blinded by
Blindness/Deafness, or marked by Guiding Bolt) are recorded in the combatant's `effects` and are not
stripped from the narration. The scope is also summarised in the rules and narrator prompts. Declared actions are read in English. Italian and Spanish are read only for a few casting, advantage, cover, lethality and action words. A catalog spell resolves only when its English name is in the declaration; a spell the parser names from another language is ruled as not resolved. Other languages are not classified by these words and rely on the parser's own entities. The payload carries this as `declaration_language`.

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

### Adventure preparation jobs

Upload and generation work still runs in-process. The API does not automatically restart a
worker after a process restart; startup changes unfinished jobs to `interrupted` and preserves
their durable recovery state. `GET /api/adventures` and `GET /api/adventures/{slug}` expose a
safe `job` projection: job kind and state, the actual stage, saved/total generation chapters,
whether retained work is retryable, and a short recovery action/explanation. The projection
never includes an outline, chapter text or manuscript content. Stage information is not an
overall percentage or an ETA.

Generation checkpoints persist the outline before chapter expansion and persist each completed
chapter before reporting it as saved. `POST /api/adventures/{slug}/retry` resumes missing
chapters; when the complete manuscript is already saved, it rebuilds the index without another
generation call. If no valid checkpoint exists, retry explicitly restarts generation. Uploaded
PDF retry reuses the retained candidate or canonical source and reruns extraction/indexing; an
inspected-preview retry includes the preview's source and extraction hashes. Repeated retry
requests while a job is active return a conflict rather than starting duplicate work.

`POST /api/adventures/{slug}/cancel` requests cooperative cancellation. It returns the safe job
projection with `cancel_requested`; cancellation takes effect at a safe checkpoint and can be
retried later when retained work is available. A cancellation request is rejected once source
publication is committing. When replacing a ready source, its last published receipt remains
available for play until a validated replacement is activated. Failed or cancelled replacement
work therefore does not make that previous source unavailable.

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
Replacement attempts stage their source and artifacts by attempt, and activation keeps the last
published index receipt available until the validated new version becomes active. If a retry fails,
the prior ready source remains ready for play; only a source with no prior published version is
reported as failed. Legacy sources without extraction evidence retain the live coverage checks
introduced by EDG-001.

For a generated source, authors can export the exact retained original Markdown locally with
`docker compose exec api python -m apps.api.services.manuscript <slug> /data/exports/<file>.md`.
The command verifies the retained file's SHA-256 before publishing the export; the output path
must be in a directory available inside the API container. It reports legacy sources and missing
or modified originals as unavailable instead of rebuilding text from index chunks. There is no
player-facing manuscript download route.

This contract uses the existing adventure JSON directories for metadata and extraction bundles,
and optional `source_pages`, `source_sha256` and `extraction_sha256` fields in Qdrant chunk
payloads. It introduces no relational database migration or separate extraction store.
