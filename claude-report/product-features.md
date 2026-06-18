# Edgar — Product features: upload campaigns, campaign library, characters (PG), sessions

This builds on the solo-play fixes (see `README.md`) to make Edgar a real product: a user
uploads their own campaign PDF, it gets embedded, and they manage campaigns, characters, and
sessions from the UI.

## Model (as the user describes it)
- **Campaign** = an uploaded adventure module (a PDF that gets embedded into its own Qdrant
  collection). You *upload a campaign*; it then appears in **Campaigns**.
- **Session** = one playthrough/save inside a campaign. A campaign has **many sessions**; you
  can start new ones and **continue any** of them whenever. (Sessions ≠ campaigns.)
- **Character (PG)** = belongs to you; engaged in at most one campaign at a time.

Under the hood: an uploaded module is an *adventure* (Qdrant collection + metadata). Opening a
campaign get-or-creates one `campaigns` DB row bound to that module, which owns its sessions,
world state, NPCs, and character assignments. The DB already supported many sessions per
campaign — no schema change was needed for that.

## What was added

### Backend
- **`POST /api/adventures/upload`** (multipart: `file`, optional `title`) — saves the PDF to the
  persistent `/data/pdfs` store and embeds it **in the background** via the existing ingestion
  pipeline (`ingestion.embed.run_pipeline`: pymupdf4llm extract → chunk → OpenAI embed → Qdrant
  upsert). Returns **202** immediately with `status: "processing"`.
- **Lifecycle tracking** in the adventure metadata JSON: `processing → ready | failed`, with
  `chunks` and `error`. `GET /api/adventures` now unions embedded collections **and**
  still-processing/failed uploads and reports `status`, so the UI can poll until ready.
- **`POST /api/adventures/{slug}/campaign`** — get-or-create the playable campaign for a module
  (idempotent per user+module), so "open a campaign" lands on its sessions.
- **`DELETE /api/adventures/{slug}`** now also removes the uploaded PDF.
- Added `python-multipart` to the api dependencies (+ lockfile) for file uploads.
- **`api` service now bind-mounts `./data`** (docker-compose.yml) so uploaded PDFs + adventure
  metadata persist across container recreates and are shared with the ingestion service.
- `GET /api/characters` already returns each character with its `current_assignment`
  (campaign id + title) — reused directly for the Characters page.

### Frontend (`/ui`)
- **Campaigns page** (landing): upload a campaign PDF with live **Embedding… → Ready/Failed**
  status (polls every 3s, stops on unmount), a card grid of all campaigns, **Open** (ready) →
  campaign hub, and confirm-gated **Delete**.
- **Campaign hub → Sessions**: clear "a campaign has many sessions" UX — start a new session
  (pick/assign a PG, 409-aware) and **continue** any existing session; delete sessions.
- **Characters (PG) page** (`/characters`): all your characters with class/level/HP and the
  campaign each is engaged in (or "Available"); create (seeded from presets) and delete.
- **Navigation**: Campaigns / Characters / Settings with active highlighting.
- The new-campaign wizard's adventure picker now filters to `ready` modules only.

## Verified end-to-end (against the live stack)
1. Onboard → 2. Upload `Goblin Caves` PDF (202) → 3. background embed → **ready (42 chunks)** in
~3s → 4. appears in campaigns list → 5. open → campaign row created → 6. create PG "Borin" →
7. assign → 8. **two sessions** created in the one campaign → 9. both listed → 10. characters
list shows "Borin → Goblin Caves" → 11. played a turn, narration grounded in the uploaded
module. UI routes `/ui`, `/ui/campaigns`, `/ui/characters` all serve; **39/39 tests pass**;
production api image rebuilt (UI + `python-multipart` baked in).

## Round 2 — opening scene, history, and AI-generated campaigns

### Opening scene ("session zero")
A brand-new session used to open as a blank chat — the player had no idea what to do, and
"continue" looked empty because no turns had ever happened. Fixed:
- **`POST /api/sessions/{id}/intro`** (idempotent) generates a grounded **opening scene**: the DM
  sets the stage from the module's **earliest pages** (read directly by page order, since
  semantic search for "the beginning" tends to return a dramatic mid-adventure scene) and ends
  by inviting the player to act. It's persisted as the session's first narration, so it also
  leads the **history** when the player later continues.
- The Play screen auto-calls it when a session has no history; the composer placeholder now
  shows example actions. (History-on-continue already worked — it was empty only because there
  was no opening; now there always is one.)

### AI-generated full campaigns (no PDF needed)
- **`POST /api/adventures/generate`** body `{title?, theme?, size}` with
  `size ∈ small | medium | large | gigantic`. The model first designs an **outline** (premise,
  setting, hook, factions, key NPCs, and N chapters where N scales with size: 3/6/12/24), then
  **expands each chapter concurrently** into runnable module text, assembles them into pages
  (page 1 = premise + opening chapter), and embeds them via the same pipeline as an upload
  (`ingestion.ingest_pages`, refactored out of `run_pipeline`). Runs as a background task with
  the same `processing → ready | failed` status.
- Result: a generated campaign is **playable identically** to an uploaded one — opening intro,
  RAG grounding, and combat all work with zero special-casing. Verified end-to-end: generated
  "The Lightkeeper's Curse" (small, 21 chunks, ~32s) → opened → created a PG → opening scene set
  in the generated village of Saltmarsh → played.
- Cost/time scale with size (gigantic = 1 outline + 24 chapter calls, bounded to 6 concurrent;
  a couple of minutes). gpt-4o-mini keeps it cheap.

### UI friendliness pass
- Campaigns page: **Generate with AI** (size selector + theme) and **Upload a PDF** tabs sharing
  one card grid + status polling; cards show the premise; primary **▶ Play** action.
- Campaign hub: Sessions tab is primary, auto-opens the new-session picker when empty, clear
  **Continue** vs **Start a new session**, with copy explaining a campaign has many sessions.
- Friendly empty states / onboarding everywhere; no raw error JSON.

## Round 3 — developer mode & a landing page

### Developer mode vs User mode
- **Backend**: the turn-stream request accepts `debug: true`, which adds one **`debug` SSE
  frame** (before `done`) carrying everything a dev needs to confirm correctness: the parsed
  **intent/entities/dice_expression**, the **RAG chunks actually retrieved** (rules + adventure,
  with collection/score/page/text and counts — the exact signal that would have caught the
  original silently-dead-RAG bug), **world flags in**, the **full combat state** (enemy
  AC/attack/damage/HP, initiative, second-wind), **per-stage timings**, and the **model**.
  (`apps/api/services/turn_runner.py`, `schemas/turn.py`, `routers/session.py`.)
- **Frontend**: an app-wide **User ⟷ Developer** toggle (persisted; amber "DEV" indicator;
  in the top nav and the Play header so it flips **mid-campaign**). In dev mode each turn shows
  a collapsible **Dev panel** (Intent, full Adjudication incl. mechanical roll log, RAG
  retrieval with empty-list red flags, world flags, full combat table, timings + model, and raw
  JSON), plus a live pipeline strip (parsing→retrieving→adjudicating→narrating→saving). User
  mode is unchanged.

### Landing page + logo
- A full-bleed hero **Landing** at `/` (Campaigns moved to `/campaigns`): a custom inline-SVG
  **EDGAR** wordmark where the **A is a volcano** (lava crater + smoke plume), a **dragon** arcs
  above the word, and **smoke curls around the E**; ember/molten palette, reduced-motion-safe
  animation. One-sentence pitch + CTA tabs into Campaigns / Characters / Settings. The compact
  logo also replaces the nav title. (`src/components/EdgarLogo.tsx`, `src/routes/Landing.tsx`.)

### My playthrough, viewable in-app
- The full solo campaign I played is restored into session 1 — read it at **`/ui/play/1`** — and
  the complete transcript with every dice roll is in `artifacts/campaign-transcript.md`
  (regenerated via `restore_playthrough.py`).

## Notes / follow-ups
- Ingestion runs in a FastAPI background task (in-process threadpool). Fine for single-user
  local use; for production/multi-user move it to a real job queue (e.g. RQ/Celery/Arq) with a
  jobs table, and cap concurrent embeds + per-user upload quotas.
- Upload status lives in the metadata JSON (survives DB resets); if you scale to multiple API
  replicas, move status to Postgres/Redis so any replica sees it.
- Still single-user (the `current_user_id` seam). Per-user isolation of uploaded campaigns +
  auth is the M0 step in `multiplayer-plan.md`.
