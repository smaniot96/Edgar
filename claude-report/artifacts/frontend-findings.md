# Edgar Frontend Review — Findings

Scope: `/Users/psmaniotto/Desktop/MyProjects/Edgar/frontend` (Vite + React 19 + Tailwind, served under `/ui`). READ-ONLY review. Cross-checked against backend SSE contract in `apps/api/services/turn_runner.py` and `apps/api/routers/session.py`.

## App map (user flow)

- `main.tsx` — `createBrowserRouter(..., { basename: "/ui" })`. Routes: `/` Home, `/new` wizard, `/campaigns/:id` Campaign, `/settings`, `/play/:sessionId` Play.
- Home (`routes/Home.tsx`) — calls `GET /api/users/me`; if 404 shows `WelcomeForm` (create user). Lists campaigns; "Resume last session" from `localStorage.lastSessionId`.
- New adventure (`routes/NewAdventure.tsx`) — 3 steps: pick adventure, name campaign (POST `/api/campaigns`), build character from preset (POST `/api/characters`, POST `/api/sessions`, PATCH session `active_character_id`), then navigate to `/play/:id`.
- Campaign (`routes/Campaign.tsx`) — tabs Sessions / Characters / NPCs / World State. Start/delete sessions, CRUD NPCs + world flags.
- Play (`routes/Play.tsx`) — header pills (title, char, HP, scene, turn-status), message list, composer. POSTs to `/api/sessions/:id/turn/stream`, consumes SSE via `useSseTurnStream` -> `readSse`.

Backend SSE contract (`turn_runner.py`): events `status {stage}`, `token {text}`, `adjudication <AdjudicationResult dict>`, `done {narration, character, current_scene_id}`, `error {detail}`. Lock contention returns **HTTP 409 JSON before the stream opens**; ended campaign returns **409** on session/turn create.

---

## CRITICAL

### C1. `adjudication` SSE event (dice, combat, HP deltas) is silently discarded
- File: `src/hooks/useSSE.ts:22-24`; consumed nowhere in `src/routes/Play.tsx`.
- The backend emits a full `adjudication` frame (`AdjudicationResult.model_dump()` — dice rolls, checks, outcomes, HP/combat changes) once per turn. The hook has an empty branch (`/* play UI ignores until a dedicated panel exists */`) and `TurnStreamHandlers` has no `onAdjudication`. The player never sees any dice, check DC, success/failure, or combat resolution — only prose.
- Player impact: For a solo player trying to *finish a campaign*, combat and skill checks are unreadable. They cannot tell why an attack hit/missed, how much damage was dealt, or what was rolled. This is the single biggest gap to "finishing" a campaign.
- Fix: Add `onAdjudication(result)` to `TurnStreamHandlers`, wire it in `useSseTurnStream`, and render a structured panel/inline card in Play (rolls, DC, outcome, HP changes, combat round/turn order).

### C2. `done` payload (`character`, `current_scene_id`) ignored; replaced by 3–4 redundant fetches with a race
- File: `src/routes/Play.tsx:150-164` (`onDone` ignores `data.character` / `data.current_scene_id`) and `:47-80` (`refreshHeader`).
- The `done` event already carries post-write `character` (incl. HP) and `current_scene_id`. Instead, `onDone` calls `void refreshHeader(sessionId)` which fires `GET /api/sessions/:id`, `GET /api/campaigns/:id`, `GET /api/characters/:id`, `GET /api/characters/:id/assignments` — 4 round trips for data already in hand.
- Race/correctness: `refreshHeader` is fire-and-forget (`void`). If the player sends the next turn quickly, two `refreshHeader` runs interleave and the header HP/scene can show a stale value (last-writer-wins on un-ordered responses). Also any error in `refreshHeader` during a turn is swallowed (the boot path catches it, but the post-turn `void` call does not), so a failed refresh silently leaves stale HP.
- Player impact: HP/scene pills can lag or show wrong numbers right after a turn — exactly the state the player needs to make decisions. Extra latency and load per turn.
- Fix: In `onDone`, update char/HP/scene pills directly from `data.character` and `data.current_scene_id`; drop the post-turn `refreshHeader`, or at least guard it against overlap and surface its errors.

---

## HIGH

### H1. No retry/handling for 409 lock contention or ended-campaign 409
- File: `src/routes/Play.tsx:129-140`.
- Backend returns **409** when a turn lock is held (concurrent turn) and **409** when the campaign is `ended`. The non-OK branch renders a raw `[error 409: {...}]` DM bubble with no retry and no distinct messaging.
- Player impact: A double-submit (Enter twice, slow network) shows a cryptic error instead of being debounced/retried. An ended campaign gives the same opaque error with no "campaign complete" framing. The player can get stuck without understanding why.
- Fix: Detect `r.status === 409`; if lock contention, auto-retry with short backoff or show "A turn is already in progress…"; if campaign ended, show a proper completion state (see H4). Disable Send while a request is in flight (it is via `busy`, but parsing the body distinguishes the two 409 causes).

### H2. SSE stream never aborted; no timeout; no cancel on unmount
- Files: `src/routes/Play.tsx:117-182` (raw `fetch`, no `AbortController`), `src/api/sse.ts:8-32` (loops until server `done`).
- The turn POST and the `readSse` reader have no `AbortSignal` and no timeout. If the backend hangs after `status: parsing` (LLM stall, dropped connection that never closes), the UI sits on "Parsing…" forever with Send disabled and no way to cancel. Navigating away during a stream leaves the reader running and may call `setLines`/`setStreamingDm` after unmount (React warning + wasted work); there is no cleanup in the `send()` path (the `useEffect` cleanup only guards boot).
- Player impact: A single stalled turn soft-locks the play screen; the only recovery is a full page reload. No "Stop"/"Cancel" affordance.
- Fix: Use an `AbortController` stored in a ref, abort on unmount and on a watchdog timeout (e.g. no event for N seconds), and expose a Cancel button while `busy`.

### H3. `error` SSE frame after streaming leaves partial narration orphaned; no scroll-to-bottom
- File: `src/routes/Play.tsx:165-172` (onError) and `src/components/MessageList.tsx`.
- (a) If tokens have already streamed and then an `error` frame arrives, `onError` pushes an `[error: …]` bubble and clears `streamingDm`, discarding the partial narration the player was reading. (b) `MessageList` has no autoscroll — as narration streams and on new messages, the container does not scroll to the bottom, so on a full message history the newest DM text and the streaming bubble are below the fold.
- Player impact: Player loses in-progress narration on error; must manually scroll every turn to read the DM's response — punishing on long campaigns and on mobile.
- Fix: On error, preserve accumulated `dmAccum` as a DM bubble plus an error note. Add a `ref` + `scrollIntoView`/`scrollTop = scrollHeight` effect keyed on `messages.length` and `streamingDm`.

### H4. No "campaign complete / ended" UI anywhere
- Files: `routes/Play.tsx` (no `status === "ended"` handling), `routes/Home.tsx:159-161` (shows raw `status` text only), `routes/Campaign.tsx` (no ended state).
- `CampaignRead.status: "active" | "ended"` and `ended_at` exist in `types.ts` and the backend, but no view treats "ended" specially. Play does not fetch/show campaign status; it only learns of "ended" via a 409 when the player tries to act. There is no banner, no "The campaign has ended" screen, no way to *mark* a campaign ended from the UI.
- Player impact: A solo player's core goal — *finishing* — has no closure UI. They cannot tell a campaign is over except by hitting an error, and cannot intentionally end one.
- Fix: In Play, read `campaign.status`; if `ended`, render a completion banner and disable the composer. On Home/Campaign, badge ended campaigns and offer an "End campaign" action.

---

## MEDIUM

### M1. "Whose turn is it" is invisible during combat
- File: `src/routes/Play.tsx` header; no combat/initiative display.
- Combat state (round, initiative order, active combatant) exists server-side (`persist_combat`, combat graph) but is never surfaced. The only turn signal is the transient status pill ("Adjudicating…"). In combat the player cannot tell whose turn it is or the round number.
- Player impact: Combat is opaque; the player can't plan actions. Pairs with C1.
- Fix: Surface combat state from the `adjudication`/`done` payloads (round, turn order, active actor) as a combat HUD shown only when combat is active.

### M2. Character sheet / stats / inventory never shown during play
- File: `src/routes/Play.tsx` (only name, level, class, HP pills).
- `CharacterRead.base_stats` and `base_inventory` and assignment `stats`/`inventory` are fetched/available but never rendered in Play. The player has no sheet, no stats, no inventory while making decisions.
- Player impact: Player can't reference their own abilities/items mid-campaign.
- Fix: Add a collapsible character-sheet panel/drawer in Play (stats, inventory, HP bar).

### M3. HP shown as text only; no low-HP / 0-HP (death) indication
- File: `src/routes/Play.tsx:74-75` (`HP x/y` pill, neutral styling).
- HP is a plain pill with no color change at low/zero HP and no death state. A dying or dead character looks identical to a healthy one.
- Player impact: Player can miss that they're at death's door; no feedback on the most safety-critical stat.
- Fix: Color the HP pill by ratio; show a clear "Defeated/Unconscious" state at 0 HP and gate the composer accordingly.

### M4. NewAdventure: failed character creation can orphan a campaign; partial-failure not unwound
- File: `src/routes/NewAdventure.tsx:212-250` (Step3 `finish`).
- `finish` does POST character -> POST session -> PATCH session sequentially. If the session POST or PATCH fails after the character (or campaign, created in Step2) is created, the wizard shows an error but leaves an orphaned campaign/character/session and offers no resume/retry — re-submitting creates duplicates.
- Player impact: Dangling half-created campaigns clutter Home; confusing onboarding.
- Fix: Make session creation idempotent or clean up on failure; or move creation server-side into one endpoint; at minimum, on retry reuse already-created IDs.

### M5. `hpMax` can become `NaN`; number input not validated
- File: `src/routes/NewAdventure.tsx:315` (`parseInt(e.target.value, 10)` with no fallback).
- Clearing the HP field yields `NaN`, which is then POSTed as `hp_max`. No client guard (only `min={1}` which the browser does not enforce on submit for typed/blank values).
- Player impact: Character creation can fail server-side or create a 0/NaN-HP character.
- Fix: `Number.isNaN` guard / clamp to >= 1 before submit.

### M6. `localStorage.lastSessionId` is global, not per-user; survives deletion
- Files: `routes/Campaign.tsx:52,103`, `routes/NewAdventure.tsx:244`, `routes/Home.tsx:76,131-139`.
- "Resume last session" reads a single global key. After switching user (Settings only edits display name, but email-based identity can change) or deleting that session/campaign, "Resume" navigates to a dead session id -> Play boot error.
- Player impact: "Resume last session" can dump the player into an error screen.
- Fix: Validate the session still exists before showing Resume (or clear the key on delete); scope key by user id.

### M7. Errors rendered as raw response bodies / `String(e)` throughout
- Files: e.g. `Play.tsx:137,177,196`, `Campaign.tsx`, `NewAdventure.tsx`, `Home.tsx`.
- Error UIs print `ApiError.body` (raw FastAPI JSON like `{"detail":"…"}`) or `String(e)` (`[object Object]`-ish / network stack). No friendly messages, no request-id surfaced (it's captured in `ApiError.requestId` but never shown).
- Player impact: Confusing, technical error text; hard to report issues.
- Fix: Parse `{detail}` from JSON bodies; map common statuses to friendly copy; show request id in a small "details" affordance.

---

## LOW

### L1. Accessibility gaps
- Files: `components/MessageList.tsx:18-21`, `components/ComposerBar.tsx`, `routes/Play.tsx` pills.
- `aria-live="polite"` is on the whole scrolling message list (will announce the entire history, not just new content); streaming tokens cause excessive announcements. Pills convey HP/scene/status with no labels for screen readers (e.g. "HP 3/12" is fine but "scene —" and the status pill aren't labeled regions). No focus management after navigation; status pill changes are not announced. Buttons rely on color (red delete) without text alternatives in some icon contexts.
- Fix: Move `aria-live` to a dedicated region for the latest DM message only; add `aria-label`/`role="status"` for the turn-status pill; ensure HP/scene are in a labeled region.

### L2. Loading/empty states are inconsistent and unstyled
- Files: `Play.tsx:213-216` ("Loading..." bubble), various `Loading…` / `null` placeholders.
- Mix of `Loading...` vs `Loading…`, plain text vs a fake message bubble. No skeletons. Empty message history in Play shows nothing (no "Your adventure begins — describe your first action").
- Fix: Standardize a spinner/skeleton; add an empty-state prompt in Play to tell the player to type their first action.

### L3. Mobile: header pills wrap, composer fixed height, no safe-area; 720px bubble cap
- Files: `Play.tsx:203-211` header `flex-wrap`, `MessageList.tsx:26` `max-w-[720px]`, `ComposerBar.tsx`.
- On narrow screens the header pills wrap to multiple rows eating vertical space; the composer textarea is `resize-y` (awkward on touch) and there's no `env(safe-area-inset)` padding (iOS home bar overlaps Send). Bubble `max-w-[720px]` is fine but combined with `p-4` can overflow on very small screens for long unbroken tokens (mitigated by `break-words`).
- Fix: Collapse pills into a compact bar/drawer on mobile; add safe-area padding; consider auto-grow textarea.

### L4. No optimistic guard against empty/duplicate sends beyond `busy`; Enter sends with IME open
- File: `components/ComposerBar.tsx:22-27`.
- `onKeyDown` sends on Enter without checking `e.isComposing` / `e.nativeEvent.isComposing`, so IME (CJK) users sending mid-composition submit prematurely. Trimming happens in `send()`, fine.
- Fix: Guard `if (e.nativeEvent.isComposing) return;`.

### L5. Adventure `cover_image` path uses leading slash, bypassing `/ui` base
- File: `src/routes/NewAdventure.tsx:60` (`src={`/${adv.cover_image}`}`).
- Image src is rooted at site origin (`/<cover_image>`), not under `/ui/`. If covers are served by the API at root this is intentional, but it is inconsistent with the SPA base and will 404 if assets are served relative to `/ui`. Verify against backend static mount.
- Fix: Confirm the cover-image serving path; use `apiBase`-relative URL if covers come from the API.

---

## Build / config

### B1. `index.html` script src is `/src/main.tsx` (absolute), not base-relative — OK in dev, relies on Vite rewrite
- File: `index.html:11`; `vite.config.ts:5` `base: "/ui/"`.
- `base: "/ui/"` is correct for production (assets emitted under `/ui/`). `%BASE_URL%favicon.svg` is correct. The dev script tag `/src/main.tsx` is rewritten by Vite; production build replaces it with hashed `/ui/assets/*`. This is fine but worth noting: the router `basename: "/ui"` and Vite `base: "/ui/"` must stay in sync — they currently are.
- No fix required; flagged for awareness.

### B2. Dev proxy only forwards `/api`; SSE works through proxy but no `changeOrigin`/timeout tuning
- File: `vite.config.ts:7-12`.
- Proxy `"/api": "http://localhost:8000"` covers both REST and the SSE stream (same path prefix), good. No explicit `ws`/timeout config; long-lived SSE generally works but Vite's proxy can buffer — verify tokens stream incrementally in dev (prod is same-origin via the API container, so unaffected).
- Fix (optional): If dev streaming feels chunky, add proxy options; otherwise none.

### B3. Bleeding-edge / likely-nonexistent dependency versions
- File: `package.json`.
- Versions look ahead of reality for a Jan-2026 cutoff: `vite@^8`, `typescript@~6.0.2`, `eslint@^10`, `@eslint/js@^10`, `react@^19.2.5`, `react-router-dom@^7.15`. If these are placeholders, `npm ci` may fail or resolve unexpectedly. (Repo history shows a recent "regenerate package-lock for Docker npm ci" fix, suggesting churn here.)
- Fix: Verify the lockfile resolves and `tsc -b && vite build` passes in CI; pin to released versions.

### B4. `VITE_API_BASE_URL` empty-string default + same-origin prod assumption
- Files: `src/api/client.ts:2`, `.env.development`.
- `apiBase = import.meta.env.VITE_API_BASE_URL ?? ""`. Empty string means same-origin; in prod the SPA is served by FastAPI at `/ui` and API at `/api` on the same origin, so this is correct. Note `??` only falls back on `undefined`; an explicitly empty env var also yields `""` — fine. No CORS handling needed by design.
- No fix required.

---

## Priority order (player trying to finish a campaign)
1. C1 — surface adjudication (dice/combat/HP changes).
2. C2 — use `done` payload for char/HP/scene; remove redundant fetch + race.
3. H4 — campaign-complete/ended UI and closure.
4. H1 — 409 lock/ended handling + retry.
5. H2 — stream abort/timeout/cancel (no soft-lock).
6. H3 — preserve partial narration on error; autoscroll.
7. M1/M2/M3 — combat HUD, character sheet, HP danger states.
8. Remaining M/L/B items.
