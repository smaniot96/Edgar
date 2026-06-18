# Edgar Backend Correctness / Bug Review

Read-only review of the Edgar D&D DM AI app. Focus: anything that breaks or
degrades a solo playthrough, especially combat and the ability to *finish* a
campaign. Findings are ordered by severity and by impact on completing a campaign.

Provider note: the agent uses OpenAI via `langchain_openai` (see
`apps/agent/src/agent/llm.py`, `db/vector/embeddings.py`), not Claude.

---

## CRITICAL

### C1. Combat advances only one actor per HTTP turn — the player's typed action is consumed by whichever actor is "current", including enemies
**File:** `apps/agent/src/agent/graph_combat.py:67-140`, `:87-107`; driven by
`apps/api/routers/session.py:223` and `apps/api/services/turn_runner.py:150`.

**Failure mode:** The combat subgraph runs exactly **one** actor's turn per
`app.ainvoke` (per HTTP request). `_get_turn_input` looks up
`initiative_order[current_turn_index]`. If that actor is `"player"`, it uses the
player's typed message; otherwise it asks the LLM to invent an enemy action.

But the *player still typed a message* for every HTTP turn. So the flow is:

- Player rolls high initiative (player first): turn 1 uses the typed action. Fine.
- Player rolls low (e.g. order = `[goblin, player]`): on the player's **first**
  combat message, `current_turn_index = 0` = `goblin`. The player's typed action
  (`"I attack the goblin"`) is **discarded**; `_get_turn_input` instead generates a
  goblin action via the LLM. The player must send a *second* message just to get
  their own turn — and that second message is what actually drives the player turn,
  meaning the text they typed for "their" attack never matches what gets adjudicated.

With N enemies the player must send N+1 messages per round, and N of those typed
messages are thrown away and replaced with LLM enemy actions. The player has no
way to know this is happening.

**Player-visible impact:** Combat feels broken and non-deterministic. The action
you type is frequently ignored and replaced by an enemy turn you didn't ask for;
you have to spam "I wait" to skip enemy turns. This alone makes combat
effectively unplayable for any encounter where the player doesn't win initiative.

**Recommended fix:** Drive the full round inside one invocation: loop through the
initiative order, taking the player's typed input only for the player's slot and
generating enemy actions for the rest, until it is the player's turn again (or
combat ends). I.e. one HTTP turn == "resolve all enemy turns up to and including
the player's next action." Alternatively, surface "whose turn it is" to the client
and prompt for input only on the player's slot.

---

### C2. There is no enemy HP / death tracking, so combat can essentially never end except by round cap
**File:** `db/postgres/models/combat_states.py:9-24` (no enemy state column);
`apps/agent/src/agent/graph_combat.py:115-121`; `apps/api/services/world_writes.py:67-95`
(`character_update` only applies to the **player** assignment).

**Failure mode:** `CombatState` stores only `initiative_order` (list of names),
`round`, `current_turn_index`, `ended`. There is no per-enemy HP. `apply_adjudication`
/ `_apply_character_update` only ever modifies the *active player character's*
assignment. Damage "dealt to the goblin" is never recorded anywhere. The
`initiative_order` is never pruned when an enemy "dies", so dead enemies keep
taking turns.

Combat can only terminate two ways (`graph_combat.py:115-121`):
1. `next_round > MAX_COMBAT_ROUNDS` (10) — a hard timeout, and
2. the adjudicator happens to put the literal substring `"combat ends"` into
   `mechanical_summary`.

There is no state telling the adjudicator the enemies are dead (no enemy HP in the
prompt), so #2 depends entirely on the LLM guessing. With the per-turn structure
(C1), the player can't reliably reduce enemies anyway.

**Player-visible impact:** Encounters drag to the 10-round cap regardless of how
well the player fights; "killed" enemies keep attacking; the player can be ground
down by zombies that are already dead. A boss fight cannot be *won* in a
mechanically meaningful way — it just times out.

**Recommended fix:** Track enemy HP in `CombatState` (e.g. a JSONB
`participants: [{name, hp_current, hp_max, ...}]`), apply enemy damage in the
adjudication writer, remove defeated enemies from `initiative_order`, and end
combat deterministically when all enemies are at 0 HP (or the player is). Feed
enemy HP into the adjudicator/narrator prompts.

---

### C3. Combat-end detection is a fragile substring match on LLM free text
**File:** `apps/agent/src/agent/graph_combat.py:117-121`.

```python
if adjudication and "combat ends" in (adjudication.mechanical_summary or "").lower():
    ended = True
```

**Failure mode:** The only narrative way out of combat is the LLM writing the exact
phrase "combat ends" into `mechanical_summary`. The adjudicator's system prompt
(`prompts.py:5-15`) never instructs it to emit this phrase, and it has no enemy-HP
context to know combat *should* end. Synonyms ("the fight is over", "the goblin
falls, ending the battle", "combat is finished") all fail to match. There is also
no structured `combat_ended` field on `AdjudicationResult`.

**Player-visible impact:** Combat that has narratively ended keeps looping (enemies
keep acting) until the round cap, or ends spuriously if the LLM happens to mention
"combat ends" in flavor text mid-fight. Unreliable either way.

**Recommended fix:** Add an explicit boolean (e.g. `combat_ended: bool`) to
`AdjudicationResult`, instruct the adjudicator to set it, and key the `ended`
transition off that plus deterministic enemy-HP checks (see C2). Drop the substring
match.

---

### C4. No campaign-completion / victory path exists — a campaign can be "ended" but never *finished*
**File:** `apps/api/routers/campaign.py:92-129` (only manual end/reopen);
`apps/api/services/world_writes.py:34-35` (scene only advances if the LLM emits a
`scene_id`); `db/postgres/models/campaigns.py:21-26` (status is just `active`/`ended`).

**Failure mode:** The only "end" is the user manually `POST`-ing the campaign-end
endpoint (an admin/lifecycle action, immediately reopenable). There is no concept
of "the adventure was completed/won." `current_scene_id` advances *only* when the
adjudicator returns a `scene_id` that (a) it pulled from adventure RAG context and
(b) differs from the current one (`apply_adjudication:34`). The adjudicator prompt
(`prompts.py:13-15`) tells it to set `scene_id` "only when the adventure context
gives you a clear scene identifier; do not invent ids." Nothing defines the final
scene, and nothing detects reaching it.

**Player-visible impact:** A solo player can play indefinitely but the game never
acknowledges *finishing* the campaign. There is no win state, no ending, no
"you have completed the adventure." From a "can you finish a campaign?" standpoint:
no, not as a first-class outcome.

**Recommended fix:** Define campaign completion (e.g. a terminal scene id or a
completion flag) in the adventure metadata; detect it in `apply_adjudication` and
set `campaign.status` (e.g. `"completed"`) with an ending narration. Distinguish
"completed" from manually "ended".

---

## HIGH

### H1. Scene progression depends entirely on the LLM emitting matching scene ids — the player can get stuck
**File:** `apps/agent/src/agent/nodes/rules_adjudicator.py:19-70`,
`apps/api/services/world_writes.py:34-35`.

**Failure mode:** `current_scene_id` only moves when the adjudicator returns a
`scene_id`. The adjudicator's prompt only sees the **rules** RAG bucket
(`rules_adjudicator.py:23-26` uses `rules_context` only; adventure context is
explicitly withheld per the docstring). But scene ids come from the **adventure**
module. So the node that is responsible for setting `scene_id` is the one node that
is *not given the adventure text containing scene ids*. It will almost never have a
canonical scene id to emit, and the prompt forbids inventing one.

**Player-visible impact:** Scenes rarely (if ever) advance; the campaign position
is effectively frozen at its starting scene, and the narrator is told "stay in
this scene unless the outcome clearly moves the party" (`prompts.py:17`). The player
can become narratively stuck at the opening location.

**Recommended fix:** Pass adventure context (or at least the available scene ids)
to whatever step decides scene transitions, or move scene-transition detection to a
component that has the adventure bucket. Consider a dedicated structured "scene
transition" decision grounded in adventure RAG.

---

### H2. `world_state_updater_node` commits a second event_log row in its own session, outside the API transaction
**File:** `apps/agent/src/agent/nodes/world_state_updater.py:15-35`; relative to
`apps/api/routers/session.py:239-249` and `apps/api/services/turn_runner.py:94-104`.

**Failure mode:** The node opens its **own** `async_session_factory()` and commits
an `EventLog(event_type="adjudication", ...)` row *inside the graph run*, before the
API has applied the source-of-truth writes or committed its own
`EventLog(event_type="narration")` row. Consequences:

- **Telemetry/state divergence:** if `apply_adjudication` later fails (e.g.
  `MissingCharacterAssignmentError`, `world_writes.py:70`) the API rolls back, but
  the adjudication telemetry row is already committed. event_log then claims an
  adjudication happened whose effects were never applied.
- **Combat double-write:** in combat, `combat_turn_node` runs
  `world_state_updater_node` for *every* actor (`graph_combat.py:96-103`), so each
  enemy turn also commits an adjudication event_log row in a separate transaction —
  more divergence and more connections.
- **Connection pressure / async:** every turn opens an extra DB connection and
  transaction independent of the request lifecycle.

**Player-visible impact:** Inconsistent history/audit; harder-to-diagnose corrupted
sessions; possible connection-pool exhaustion under load. Not immediately visible to
a solo player but undermines state integrity.

**Recommended fix:** Either drop this node's own write and let the API persist all
event_log rows in the request transaction, or pass the request session/handle
through and write within it. Telemetry should not commit independently of the
source-of-truth writes.

---

### H3. `combat_state` is persisted even when no real combat ran, and combat HP writes share the player-only writer
**File:** `apps/api/services/turn_runner.py:85-104`, `apps/api/routers/session.py:228-237`.

**Failure mode (initiative on a non-attack):** Intent classification
(`input_parser`) routes to combat purely on `intent == "combat"`. The initiative
node seeds combat from `ParsedInput.entities` targets, defaulting to `"enemy"` if
none (`graph_combat.py:45`). A player message merely *mentioning* a fight ("I'd
rather avoid combat with the guards") can be classified `combat`, spin up an
initiative order including a phantom `"enemy"`/`"guards"`, and persist a
`CombatState` row — putting the session into a combat loop the player never wanted
and from which the only exits are C2/C3.

**Player-visible impact:** Spurious combat encounters triggered by intent
misclassification, then no clean way out (see C2/C3). Very disruptive to a solo
run.

**Recommended fix:** Require explicit confirmation/targets before seeding
initiative; do not default targets to `"enemy"`; allow a "flee/avoid" path that
ends or never starts combat.

---

### H4. SSE combat path drops `MissingCharacterAssignmentError` distinction and other persist errors after streaming tokens
**File:** `apps/api/services/turn_runner.py:160-167` and `:84-114`.

**Failure mode:** In `_persist_turn`, only `MissingCharacterAssignmentError` is
caught-and-re-raised; any other exception during `persist_combat` /
`apply_adjudication` / commit propagates out of the generator. By that point tokens
and an `adjudication` event have already been streamed to the client, but the
`done` frame is never sent and the writes were rolled back (or partially applied).
The client shows narration that was never persisted.

**Player-visible impact:** The player sees a narrated outcome (damage taken, door
opened) that silently did not save; on the next turn the state is as if it never
happened. Confusing and erodes trust in persistence. Divergence between the sync
endpoint (which surfaces a 5xx) and the SSE endpoint.

**Recommended fix:** Wrap the persist in a try/except that emits a structured
`error` SSE frame for all failure types, and make clear to the client that the turn
did not commit (ideally persist before streaming, or buffer and commit before the
first token).

---

## MEDIUM

### M1. `npcs` is loaded and passed into agent state but never declared or consumed — NPC context is silently dropped
**File:** `apps/api/routers/session.py:67-79` (sets `initial_state["npcs"]`);
`apps/agent/src/agent/state.py:15-40` (no `npcs` key); no node reads `npcs`.

**Failure mode:** `_load_turn_initial_state` queries NPCs and builds
`npc_summaries`, but `AgentState` has no `npcs` field and neither the adjudicator
nor the narrator reads it. The data is computed every turn and thrown away.

**Player-visible impact:** The DM has no awareness of campaign NPCs or their
dispositions; recurring NPCs won't be referenced consistently. Wasted DB query each
turn.

**Recommended fix:** Add `npcs` to `AgentState` and include it in the narrator
(and possibly adjudicator) prompt, or remove the dead query.

---

### M2. Round counting is suppressed once `ended` is set, recording a stale round
**File:** `apps/agent/src/agent/graph_combat.py:113-127`.

**Failure mode:** `next_round = round_num + 1 if next_index == 0 else round_num`,
then `ended = next_round > MAX_COMBAT_ROUNDS`, then it writes
`"round": next_round if not ended else round_num`. On the turn that triggers the
cap, the round stored reverts to the pre-increment value, so the final persisted
round is off-by-one relative to the turn that actually ended combat. Combined with
C1/C2 the round counter is unreliable as a record.

**Player-visible impact:** Minor; mainly affects any UI/telemetry showing the round
number and the exact turn combat ended.

**Recommended fix:** Compute round/index/ended consistently and store the true
round on the ending turn.

---

### M3. Memory summarizer in SSE path runs but its updated `messages` are never persisted; summaries are recomputed from raw event_log every turn
**File:** `apps/api/services/turn_runner.py:209-213`, `apps/agent/src/agent/nodes/memory_summarizer.py`,
`apps/api/services/session_messages.py:10-37`.

**Failure mode:** `messages` are rebuilt each turn from the last 10 narration rows
in event_log (`session_messages.py:14`). The summarizer compresses the in-memory
`messages` list, but that compressed list is never written back; only raw
`player_input`/`narration` go to event_log. So the summary is discarded and
recomputed (an extra LLM call) every turn once the threshold is hit, and early-game
context beyond the 10-row window is permanently lost regardless of summarization.

**Player-visible impact:** The "story so far" never actually preserves old context
across turns; the DM forgets events older than ~10 turns. Extra cost/latency per
turn. (The summarizer is effectively a no-op for its stated purpose.)

**Recommended fix:** Persist the rolling summary (e.g. a `summary` event_log row or
a session column) and seed `messages` from it, rather than always reading raw rows.

---

### M4. Combat narration may be empty and the player is never told combat ended
**File:** `apps/agent/src/agent/graph_combat.py:108-140`; `apps/api/services/turn_runner.py:154-166`.

**Failure mode:** When the subgraph runs an *enemy* turn (C1), the `narration`
returned is for the enemy action; when `ended` flips, nothing appends an explicit
"combat is over, you may continue" message. The `ended` flag is stored but never
surfaced in `TurnResponse`/`done` payload in a way the client clearly consumes
(`session.py:256-262` returns `combat_state` only on the sync endpoint; the SSE
`done` payload omits `combat_state` entirely — `turn_runner.py:110-114`).

**Player-visible impact:** The player has no clear signal that combat ended or that
they're back to normal exploration; the SSE client can't even see `combat_state`.

**Recommended fix:** Include `combat_state` (and an explicit `combat_ended` /
"resume exploration" cue) in the SSE `done` payload, and narrate combat resolution.

---

## LOW

### L1. Dice parser rejects whitespace inside expressions; LLM-produced notation may fail
**File:** `tools/dice.py:19,37-44`; consumed at `rules_adjudicator.py:30-35`.

**Failure mode:** `_DICE_PATTERN = ^(\d+)d(\d+)([+-]\d+)?$` with `fullmatch` after a
single outer `.strip()`. Common LLM outputs like `"2d6 + 3"`, `"1d20 +5"`, `"d20"`
(no leading count), or uppercase `"1D20"` all raise `ValueError`, which the
adjudicator turns into `{"error": ...}` → a 503 for that whole turn
(`rules_adjudicator.py:34-35`).

**Player-visible impact:** A single malformed dice string from the LLM 503s the
entire turn instead of degrading gracefully. Intermittent turn failures during
attacks/skill checks.

**Recommended fix:** Normalize whitespace and case, allow an implicit count of 1
(`d20` → `1d20`), and on parse failure fall back to no-dice adjudication rather than
failing the turn.

### L2. Enemy-action and player "I wait" fallbacks make initiative meaningless for flavor
**File:** `graph_combat.py:74-84`. Minor: enemy actions are free-form LLM text with
no tie to mechanics or enemy HP; player default `"I wait."` is silently substituted
when `player_input` is empty. Low impact given the larger combat issues above.

### L3. `MAX_DICE`/`MAX_SIDES` caps (100/100) are fine, but `parse_dice` allows e.g. `1d2` minimum sides=2 only — `d1`/`d100`+ rejected. Working as intended; noted for completeness. No action needed.

### L4. `roll` total can be negative if a large negative modifier is supplied (e.g. `1d4-10`). Not currently a correctness problem since HP is clamped at the writer (`world_writes.py:75`), but a negative "damage" total is nonsensical if ever surfaced directly. Low.

---

## Summary of completion-blocking issues
The campaign **cannot be meaningfully finished**, and combat is **broken**, due to
a cluster of interacting issues:
- **C1** (player action consumed by enemy turns) + **C2** (no enemy HP / death) +
  **C3** (substring combat-end) make combat unwinnable and frustrating.
- **C4** (no completion path) + **H1** (scene ids never advance because the
  deciding node lacks adventure context) mean the story never progresses to or
  recognizes an ending.

Fixing C1–C4 and H1 is the priority for a playable, finishable solo campaign.
