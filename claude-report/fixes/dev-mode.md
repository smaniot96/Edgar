# Developer mode vs User mode (frontend)

Adds an app-wide, persisted Developer/User mode to the Edgar SPA. Developer mode
reveals a per-turn "see everything" dev panel and a live pipeline indicator so
correctness can be verified while testing. Toggleable any time, including
mid-campaign on the Play screen.

## Files

- `src/dev/ModeContext.tsx` (new): `ModeProvider` + `useMode()` hook. Mode is
  `"user" | "dev"`, default `"user"`, persisted to localStorage key `edgar_mode`.
  Context re-renders all consumers on change.
- `src/dev/ModeToggle.tsx` (new): `ModeToggle` (compact User/🔧 Developer switch,
  amber-tinted in dev) and `DevBadge` (amber "DEV" badge, only visible in dev).
- `src/dev/DevPanel.tsx` (new): the core deliverable. Collapsible panel rendered
  under each DM turn in dev mode. Sections: Intent (parsed_input intent/entities/
  dice_expression), Adjudication (success/dice_result/damage/mechanical_summary/
  character_update hp_delta+conditions+inventory/flags_set/flags_cleared/scene_id),
  RAG retrieval (rules + adventure chunk lists with collection/score/page/text;
  empty lists highlighted red as the #1 correctness red flag), World flags (in),
  full Combat state table (per-combatant ac/attack_bonus/damage_dice/hp/alive,
  initiative_order, round, outcome, second_wind_used), Timings + model, and a raw
  debug JSON `<details><pre>`. Shows "no debug captured for this turn" when a DM
  line has no debug/adjudication (e.g. history or user-mode turns). Monospace for
  raw values; mobile-friendly (max-width, overflow-x on tables).
- `src/api/types.ts`: added `DebugPayload`, `RetrievedChunk`, `ParsedInput`,
  `DebugTimings`, `CharacterUpdate`; extended `AdjudicationResult` (flags_cleared,
  typed character_update), `Combatant` (display_name/ac/attack_bonus/damage_dice),
  `CombatState` (second_wind_used/initiative_order).
- `src/hooks/useSSE.ts`: added optional `onDebug?(payload)` handler; dispatches the
  new `debug` SSE event to it.
- `src/main.tsx`: wrapped `RouterProvider` in `ModeProvider`.
- `src/App.tsx`: top-nav `ModeToggle` + `DevBadge`; thin amber top border shown in
  dev mode as an at-a-glance indicator.
- `src/components/MessageList.tsx`: `Line` extended with `debug?` and `isTurn?`;
  renders `DevPanel` under DM turns when `isDev`.
- `src/routes/Play.tsx`: uses `useMode`; sends `debug: mode === "dev"` in the turn
  POST body; captures the `debug` payload and stores it on the DM `Line`; marks DM
  lines `isTurn`; Play header carries the `ModeToggle` + `DevBadge` (mid-campaign
  toggle); dev-only "Pipeline" strip highlights the live stage (parsing →
  retrieving → adjudicating → narrating → saving) from `onStatus`.

## Behavior

- User mode: identical to before; no panels/badges; adjudication roll chip kept.
- Dev mode: panels appear for DM turns; toggling user→dev reveals captured data,
  dev→user hides it. New turns sent in dev mode request `debug:true`. Past/user-mode
  turns show "no debug captured for this turn" (expected).

## Verify

`npm run build` (tsc -b && vite build) succeeded with no type/build errors.
