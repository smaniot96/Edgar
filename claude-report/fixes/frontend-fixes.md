# Frontend Solo-Play Fixes

Scope: Surface the new backend SSE/turn contract in the React SPA (`frontend/src`). Backend untouched. `npm run build` (`tsc -b && vite build`) **succeeds**.

## Changes

### `src/api/types.ts`
- Added `AdjudicationResult`, `Combatant`, `CombatOutcome`, `CombatState`, `TurnCharacter`, and `TurnResult` interfaces (after `CharacterAssignmentHistoryRead`, ~line 99+). These mirror the new `adjudication` frame and the `done`/sync turn payload (`character`, `current_scene_id`, `combat_state`, `campaign_complete`). Why: typed access to the new fields without `any`.

### `src/hooks/useSSE.ts`
- Added `onAdjudication(result: AdjudicationResult)` to `TurnStreamHandlers` and wired the previously-empty `adjudication` event branch to call it (was a no-op comment). Why: C1 — the adjudication frame was silently discarded.

### `src/components/MessageList.tsx`
- Extended `Line` with optional `adjudication` and `combatOutcome` fields.
- Added `RollChip` sub-component: compact inline chip on a DM message showing Success/Fail, `🎲 dice_result`, `N dmg`, and `mechanical_summary` (no raw JSON). Why: C1.
- Added a combat-outcome banner (Victory/Defeat/Fled) rendered under the DM message when combat ended this turn. Why: H4.
- Added autoscroll: `endRef` + `scrollIntoView` effect keyed on `messages`/`streamingDm`. Why: H3.
- Moved `aria-live="polite"` off the whole list onto the latest DM bubble + streaming bubble only (L1 nicety while reworking the file).
- Tightened the `messages` prop type to `Line[]` (only caller, Play, passes `Line[]`).

### `src/routes/Play.tsx`
- **C2**: HP is now structured `Hp { current, max }` state. Added `applyTurnResult(TurnResult)` which updates HP, char pill, scene pill, combat state and campaign-complete **directly from the `done` payload**. Removed the post-turn `void refreshHeader(sessionId)` and the non-OK-branch `refreshHeader` call (eliminating the 4-fetch race). `refreshHeader` is retained for the initial boot load only and now also flips `campaignComplete` if `campaign.status === "ended"`.
- **M3**: New `HpPill` component colors HP amber at <=25% and red + "Defeated" at 0; "HP —/—" when unknown.
- **M1**: New `CombatHud` panel (shown only when `combat_state` present and not ended) lists non-player combatants as `name hp_current/hp_max` with the round number; dead combatants struck through, low-HP enemies highlighted.
- **H4**: When `campaign_complete` is true (or campaign status ended), shows a celebratory "🏆 Campaign Complete" banner and disables the composer (`disabled={busy || campaignComplete}`). The 409 "has ended" case sets the same complete state with a clear message.
- **H1**: Non-OK turn response now branches on status 409 — "has ended" => campaign-complete state + message; otherwise => "A turn is already in progress — please wait a moment and retry." and the draft is restored so the player can resend. Other errors parse `{detail}` from JSON into a friendly bubble instead of raw `[error N: {...}]`.
- **H3**: `onError` now preserves accumulated partial narration as a DM bubble (with any adjudication chip) before appending a friendly error note, instead of discarding it.
- Adjudication captured per-turn (`pendingAdj`) and attached to the DM line on `done`/error so the roll chip renders inline.
- Guarded `send()` against re-entry when `busy` or `campaignComplete`.

## Not done (out of scope of the listed items)
- H2 (abort/timeout/cancel), M2 (character sheet), M4–M7, L2–L5 remain from the audit.

## Build note
`node_modules` had been populated for Linux ARM64 (Docker), so `rolldown` (vite 8) was missing the darwin-arm64 native binding on this Mac. Installed `@rolldown/binding-darwin-arm64` with `--no-save` (transient, local-only; does not change `package.json`/lockfile — CI/Docker already ships the Linux binding). After that, `npm run build` passes: `tsc -b` clean, vite emits `dist/` (321 kB JS / 13 kB CSS).
