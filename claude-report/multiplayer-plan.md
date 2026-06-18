# Edgar — Multiplayer Plan (multiple players, one shared table)

Goal: take Edgar from **one solo player** to a **party of players sharing one campaign and one live session** — the AI is the DM, several humans each control their own character at the same table, and everyone sees the story unfold in real time.

This is a design + phased roadmap, written against the codebase as it stands after the solo fixes.

---

## 1. Where the codebase already helps

The data model was, encouragingly, *partly* built for this:

- **`characters` + `character_assignments`** — per-character state (HP, conditions, inventory) is already keyed by `(character_id, campaign_id)` on `CharacterAssignment`, not jammed onto the session. Multiple characters can already be assigned to one campaign. This is the right shape for a party.
- **`world_flags`** and scene are **campaign/session-scoped**, i.e. shared — correct for a shared world.
- **`npcs`** are campaign-scoped — shared.
- **Redis per-session turn lock** already serializes turns; it becomes the seed of the multiplayer "turn token".
- **`users`** exist; `create_session` already validates a character's assignment.

So the *world* is already shareable. The gaps are: **identity/ownership, a party/membership concept, real-time fan-out to many clients, and turn/combat orchestration across multiple human actors.**

---

## 2. Gaps to close

1. **Auth & ownership** — there is no real authentication; `current_user_id()` returns "the first user", and most mutating routes don't check ownership at all. Multiplayer is impossible (and unsafe) without per-user identity and per-campaign authorization.
2. **Single-character session** — `sessions.active_character_id` assumes one PC. A session needs *a party* (N characters, each owned by a different user).
3. **One-to-one streaming** — SSE today is request→single stream. The acting player gets the narration; nobody else sees anything. We need a **session broadcast channel** so all members receive narration + state updates live.
4. **Turn arbitration** — with one shared world, two players can't both mutate it at once. We need explicit turn-taking: who may act now (free-form spotlight out of combat; initiative order in combat), enforced server-side.
5. **Multi-PC combat** — the combat subgraph models one player + enemies. It must model **multiple player combatants**, each acting on input from a different user, with the agent auto-running enemy turns.
6. **Agent state** — the agent takes a single `character`. It needs the **full party roster** so the DM narrates to/for everyone and adjudication targets the right PC.

---

## 3. Target model

```
User ─< CampaignMembership >─ Campaign ─< Session
 │                               │            │
 └─ owns ─ Character ─ CharacterAssignment ───┘ (party = the session's assigned characters)

Session has: party (N characters), turn_state (whose spotlight / initiative), combat_state (multi-PC)
Live sync: each Session = a Redis pub/sub channel; every member holds one SSE/WebSocket subscription.
```

### Data model changes
- **`campaign_memberships`** (new): `(campaign_id, user_id, role ∈ {dm_owner, player}, character_id, joined_at)`. Owner creates the campaign; players join via invite.
- **`session_participants`** (new) *or* reuse assignments: which characters are seated in this session, plus per-player connection/presence.
- **`sessions`**: add `turn_state` JSONB — `{mode: "free"|"combat", active_character_id, spotlight_order, ...}`. Keep `active_character_id` meaning "who has the spotlight right now".
- **`combat_state.combatants`** already supports N entries; extend player entries to carry `character_id` and `controller_user_id`.
- **Invites**: `campaign_invites` (token, campaign_id, expires_at) for join links.

---

## 4. Turn & combat orchestration (the hard part)

### Out of combat — "spotlight" turn token
- The session has a single **active actor** at a time (the Redis turn lock, promoted to a per-session token with an owner = the active `character_id`).
- Default policy: **free spotlight** — any seated player may "grab the spotlight" if it's free; their turn runs, streams to everyone, then releases. Optionally a **round-robin** mode for orderly tables.
- Non-active players' input is disabled (or queued) client-side; the server rejects a turn from a non-holder with a clear 409 ("It's Alice's turn").
- The DM narration addresses the party; the acting PC is the subject of adjudication.

### In combat — initiative across the whole table
- On encounter start, roll initiative for **every PC and every enemy** into one order (the combat subgraph already builds an ordered roster — extend it to include all party members).
- Resolve the round **actor by actor**, but pause on each *player* combatant for that specific user's input (drive via the turn token = the current combatant's `controller_user_id`). Enemies in between are resolved automatically (as today). This generalizes the current "one round per HTTP turn" to "advance to the next human actor, auto-running enemies and already-acted PCs".
- End conditions unchanged (all enemies down → victory; all PCs down → defeat/subdued; flee; stalemate), evaluated across the whole party.
- Down-but-not-out: with multiple PCs, a fallen PC can be revived by an ally (Cure Wounds / Healer's Kit) instead of the solo "subdued at 1 HP" shortcut — restore proper 5e death-save semantics now that there's a party to help.

### Concurrency
- The per-session token guarantees **one mutation in flight per session**, so the shared world stays consistent without distributed locking beyond Redis.
- Across API workers, the token + pub/sub must be Redis-backed (already are / will be).

---

## 5. Real-time sync

- Promote streaming from per-request SSE to a **per-session channel**:
  - On connect, each member subscribes (SSE *or* WebSocket) to `session:{id}` events.
  - The acting player's turn publishes `status`/`token`/`adjudication`/`done` frames to the channel via **Redis pub/sub**; the API fans them out to all subscribers.
  - Add presence events (`joined`, `left`, `turn_changed`, `combat_started/ended`) so clients can render "whose turn" and the party roster live.
- WebSockets are the better long-term fit (bidirectional: input + presence + heartbeats); SSE-per-session + a small POST for input is a faster interim step that reuses the existing SSE code.
- Keep the existing event schema; just add a `channel`/`actor` envelope.

---

## 6. Frontend

- **Lobby**: create/join campaign (invite link), pick/assign your character, ready-up, start session.
- **Party panel**: all PCs with live HP/conditions; highlight the active actor; show enemies/round during combat (the solo combat HUD generalizes to N).
- **Turn affordance**: input enabled only on your spotlight/initiative turn; otherwise "Waiting for Alice…", with the shared narration streaming to everyone.
- **Reconnect**: rejoin a session and replay recent narration (already persisted in `event_log`).

---

## 7. Infra & scaling

- **Auth**: JWT/OAuth (or a managed provider); middleware that resolves the user and a dependency that authorizes campaign membership on every route.
- **Statelessness**: move all session liveness (token, presence, pub/sub) to Redis so multiple API replicas can serve any member. Today the API holds a pooled DB connection for the whole SSE stream — fix that (stream off a short transaction; the platform review flagged connection-exhaustion under concurrency) **before** fan-out multiplies connections.
- **Cost controls**: per-user and per-campaign OpenAI spend caps + rate limits become essential once N players each generate turns.
- **Idempotency/ordering**: tag turns with the token holder + a monotonic sequence so a laggy client can't replay or reorder a turn.

---

## 8. Phased roadmap

| Phase | Outcome | Key work |
|------|---------|----------|
| **M0 — Foundations** | Safe multi-user base | Real auth; authorize every route by campaign membership; rotate the leaked key; rate-limit + spend cap; fix the long-lived DB connection on the SSE path. |
| **M1 — Party model** | A campaign has members & a party | `campaign_memberships`, invites, seat N characters in a session; agent state carries the **party roster**; narration/adjudication address multiple PCs. |
| **M2 — Live shared session** | Everyone sees the story in real time | Per-session Redis pub/sub channel; SSE/WebSocket fan-out; presence + `turn_changed` events; reconnect/replay. |
| **M3 — Multiplayer turns & combat** | Real tabletop turn-taking | Spotlight token out of combat; initiative across PCs+enemies; combat subgraph advances actor-by-actor pausing for each human; ally revival / death saves. |
| **M4 — Polish** | Pleasant to run | Lobby UX, DM controls (kick/skip/pause), spectators, per-table pacing options, observability/metrics, load test. |

**Suggested first slice (thin vertical):** M0 auth + M1 two-character party + M2 SSE fan-out + M3 free-spotlight out of combat. That gets two friends playing a shared scene together; combat-initiative multiplayer (the most intricate piece) follows.

---

## 9. Risks & calls to make

- **Turn policy is a product decision** — strict round-robin vs. free spotlight vs. DM-granted spotlight. Recommend *free spotlight out of combat, strict initiative in combat*.
- **Latency** — a turn is several seconds of LLM time during which the table waits. Mitigate with streaming (everyone watches it generate) and by letting players queue intended actions.
- **Death semantics** — solo uses "subdued at 1 HP"; multiplayer should restore real death saves + ally aid (more dramatic and fair with a party).
- **Cost scales with players** — caps and a clear billing model are mandatory before opening it up.
