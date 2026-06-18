# Product UI: Campaigns, Sessions, Characters

Reworked the React SPA (`frontend/`, React 19 / Router v7 / Vite / Tailwind, served at
`/ui`) so the product model — **Campaign** (uploaded adventure module) → many
**Sessions** → **Characters (PG)** — is clear and fully navigable. Build is green.

## Files changed

### `src/api/types.ts`
- Extended `AdventureRead` with the real upload contract fields: `status`
  (`processing|ready|failed`), `chunks`, `source_filename`, `error`, `created_at`
  (added `AdventureStatus` type).
- Added `CharacterAssignmentRef` and `CharacterListItem` (the `GET /api/characters`
  shape, incl. `current_assignment`).

### `src/api/client.ts`
- Added `errorMessage(err)` — pulls a clean message out of `ApiError` (parses
  FastAPI `{detail}` when present); used everywhere instead of raw error bubbles.
- Added typed API helpers: `listAdventures`, `uploadAdventure` (multipart),
  `deleteAdventure` (sends `X-Confirm-Delete: yes`), `getOrCreateCampaign`
  (`POST /api/adventures/{slug}/campaign`), `listCharacters`,
  `listCharacterPresets`, `createCharacter`, `deleteCharacter`, `listSessions`,
  `createSession` (optional `active_character_id`), `assignCharacterToCampaign`.

### `src/routes/Home.tsx` — now the **Campaigns** page (primary landing)
- Kept first-run onboarding (`WelcomeForm` → `POST /api/users`).
- **Upload campaign** card: PDF + optional title → `uploadAdventure`. On success
  the new card appears immediately in "Embedding…" state.
- **Polling**: `GET /api/adventures` every 3s while any adventure is
  `processing`; interval auto-stops when none remain pending and on unmount
  (cleared via ref in the effect cleanup).
- Responsive card grid with status badges (Embedding… / Ready / Failed). Ready
  cards have **Open** → `getOrCreateCampaign` → navigate `/campaigns/{id}`
  (button shows "Opening…"/disabled while in flight, and is disabled until
  ready). Failed cards show the `error`. Every card has **Delete** behind a
  confirm dialog. Loading/empty/error states throughout.
- Kept a small link to the existing `/new` wizard as an alternate path.

### `src/routes/Characters.tsx` — new `/characters` page
- Lists all user characters (name, class, level, HP) with a badge showing the
  campaign they're engaged in (links to `/campaigns/{id}`) or **Available**.
- **Create character** form seeded from `/api/character-presets` (same pattern as
  the wizard) → `createCharacter`. Per-row **Delete** with confirm. Full
  loading/empty/error states.

### `src/routes/Campaign.tsx` — campaign hub
- Rewrote the **Sessions** tab: explicit copy that "a campaign has many
  sessions"; split into "Start a new session" and "Existing sessions".
- New-session flow opens a character picker (`listCharacters`), assigns the PG to
  the campaign first when needed (`assignCharacterToCampaign`), then
  `createSession(campaignId, characterId)` and navigates to `/play/{id}`. A 409
  (PG active elsewhere) surfaces its message via `errorMessage`; characters
  engaged in another campaign are shown disabled with their current campaign.
  Also supports "Start without a character".
- Existing sessions are listed (sorted newest-first) with **Continue** →
  `/play/{id}` and **Delete**.
- Characters tab now links to the `/characters` roster; back-links relabeled
  "← Campaigns". Other tabs (NPCs, World State) unchanged.

### `src/App.tsx`
- Top nav now: **Campaigns** (`/`, `end`), **Characters** (`/characters`),
  **Settings**, with active-route highlighting (existing `navClass`).

### `src/main.tsx`
- Registered `/characters` route and a `/campaigns` alias to the Campaigns page.
  Existing `/new`, `/campaigns/:id`, `/play/:sessionId` routes preserved so
  Play/Campaign keep working.

## Verify
`npm run build` (`tsc -b && vite build`) **succeeds** with no type/build errors.
The rolldown darwin-arm64 workaround was **not** needed.

## Notes
- Play screen untouched except that it's still reached via `/play/{sessionId}`.
- Polling is the only timer; it is cleaned up on unmount and stops once all
  uploads are ready/failed.

---

# Update (2026-06-18): AI campaign generation + friendliness pass

Added an AI-campaign-generation flow and a usability pass after feedback that the
UI was "still not really user friendly." Build is green (`npm run build`).

## Files changed

### `src/api/types.ts`
- Added `AdventureSize` (`small | medium | large | gigantic`),
  `AdventureGenerateRequest` (`{title?, theme?, size}`) and
  `AdventureGenerateResponse` (`{slug, title, status:"processing"}`) to mirror the
  new `POST /api/adventures/generate` (202) contract.

### `src/api/client.ts`
- Added `generateAdventure(body)` — POSTs the generate request and returns the
  processing stub. Reuses `apiFetch`/`json`/`ApiError` like the other helpers.

### `src/routes/Home.tsx` (Campaigns landing)
- New `CreatePanel` with two tabs: **✨ Generate with AI** and **Upload a PDF**,
  replacing the bare upload card.
- `GenerateForm`: optional Title, optional Theme/premise (placeholder "a haunted
  lighthouse on a cursed coast"), and a 4-tier Size selector with one-line blurbs
  (Small/Medium/Large/Gigantic, the last noting it takes a few minutes). On submit
  it optimistically inserts a `processing` card and lets polling flip it to Ready.
- Generate and Upload now share one `handleCreated` → same card grid and the same
  existing 3s `GET /api/adventures` polling. Status pill/label for in-flight cards
  is the generic "Preparing…" (no kind flag is exposed by the API).
- Friendliness: card primary action relabelled **▶ Play** (was "Open"), shows a
  "Building your campaign…" hint while processing and "Unavailable" on failure;
  "Resume last session" relabelled with ▶. Empty state is now a dashed call-to-action
  box ("No campaigns yet — generate one with AI or upload a PDF, then click Play").
  Reworded the welcome/onboarding copy to explain Edgar up front. Campaigns grid
  given a "Your campaigns" heading for consistency.

### `src/routes/Campaign.tsx` (campaign hub)
- Sessions tab (already the default) now auto-opens the "Start a new session" picker
  when the campaign has no sessions yet, so the path to Play is immediate. Split out
  `loadCharacters`; effect keyed on `campaignId`.
- Clearer copy: explains a session is one saved playthrough and a campaign can have
  several; picker explains character assignment / "start without one"; **Continue**
  and "Start a new session" buttons enlarged and given ▶ affordance; friendlier
  empty state.

### `src/routes/Characters.tsx`
- Replaced the terse empty state with a guidance box explaining how characters are
  used (chosen when starting a session, or created inline from a campaign).

## Notes
- Kept `Play.tsx` intro auto-generation untouched.
- All errors continue to flow through `errorMessage()` (ApiError.message / parsed
  `detail`); no raw JSON is shown.
- No new dependencies; matches the existing Tailwind dark theme and API patterns.
