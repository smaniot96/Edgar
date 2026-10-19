# Party combat (EDG-049)

One fight shared by several human-controlled heroes (multiplayer M3). Two or more heroes at a table
(the spotlight holder and the seated players) fight one encounter. A solo table, or a table with one
hero, runs the classic one-hero fight exactly as before: its code path, dice order, text and stored
rows are unchanged (the round golden in `tests/fixtures/combat/round_golden.json` is untouched).

Code: `apps/agent/src/agent/party_combat.py` (engine), `graph_combat.py` (`combat_round_node`),
`apps/api/services/spotlight.py` (the gate), `apps/api/services/turn_runner.py` (persistence),
`apps/api/services/session_broadcast.py` (events). Tests: `tests/agent/test_party_combat.py` (engine,
scripted dice) and `tests/api/test_party_combat_api.py` (routes, Postgres, Redis fake).

## Model

* **One initiative order.** Every hero and every foe rolls `d20 + bonus` (each hero its own Dexterity
  modifier). Order: higher total, then higher bonus, then heroes before foes, then the lower character
  id. The order is persisted on the combatants; heroes are rows with `character_id`.
* **One human turn per request.** The request of the hero whose slot is next resolves that hero's
  slot, then the engine runs every later slot by itself (foes, heroes that cannot act, a hero that is
  down, a hero who left) up to the next hero who can act, and pauses there. The pause point is the
  existing `combat_states.current_turn_index` column; nothing else is stored. Rounds advance when the
  order wraps, so one request can cross a round boundary. No typed action is consumed by a foe slot.
* **Per-hero state.** Each hero's row carries its hit points, conditions (`effects`), concentration,
  reaction, action economy and death record. `GET /sessions/{id}/combat` and `done.combat_state` show
  them per hero (`character_id`, `fled`, `death_saves`); the sheet itself (inventory, slots) stays
  private to its controller.
* **Opening.** The hero whose message starts the fight triggers it. Initiative is rolled for everyone
  and the slots ahead of the first hero who can act resolve. If the opener is that hero, the
  declaration is rolled now. Otherwise the opener's declaration is **not** rolled (it is not their
  turn): the reply says whose turn it is, and the opener acts in their own slot. The encounter budget
  grows by one foe per extra hero (at most 6), with unchanged per-foe quality.

## Rules the engine owns

| Topic | Rule |
| --- | --- |
| Foe targeting | A foe picks one standing hero per turn with one engine die `1dN` over the standing heroes in initiative order. With one standing hero no die is rolled. When its target falls, the rest of its attacks go to another standing hero. A foe never attacks a hero at 0 HP (as in a solo fight). Lair actions hit every standing hero, each with its own save against one shared damage roll. |
| Reactions, economy | Per hero. A hero's reaction returns at the start of its own slot; a readied attack fires for any hero holding one against the foe that closes in. |
| Down hero | Death saves mode: the hero's death save is rolled automatically at the start of its own slot, whoever else is acting. The subdued mode leaves a downed hero unconscious and at 1 HP when the fight ends. Dead heroes (record `dead`) take no more turns and are recorded in the story ledger once. |
| Ally healing | A healing spell (Cure Wounds, Healing Word) or healing potion that names another hero restores that hero (engine dice, capped at its maximum). A hero brought above 0 HP is conscious again with its death record cleared. A dead hero is beyond healing. No range is tracked between heroes. |
| Flee | A hero that flees leaves the fight alone; the others fight on. |
| End | Victory: no foe stands (every living hero who did not die shares the milestone, once, in milestone campaigns). Defeat: no hero can act and none is dying. `fled`: a hero escaped and none can act, or the round cap (12) was reached. Downed heroes are left at 1 HP in the subdued mode. |
| Departed hero | A hero whose seat was removed leaves the fight (`fled`); the order carries on without it. |

## Who may act (server side)

While a party fight is running, the **initiative holder** is the turn holder, not the free-roam
spotlight. `sessions.active_character_id` is kept equal to the hero whose slot is next, and
`spotlight_version` is the gate: a turn quoting an older version is refused `spotlight_stale`.

* A turn from anyone but the holder's controller is refused `409 not_your_spotlight` before any
  model call and with nothing spent; the stored fight is unchanged.
* Admission runs under the per-session turn lock and a row lock; the commit re-checks the version and
  holder on the locked row (`apply_turn_commit`), so two simultaneous submissions yield one winner and
  a stale admission cannot commit after the turn passed.
* Every committed combat turn bumps the version and passes the turn to the next hero who can act
  (`combat_turn_passed`, or `combat_turn_continues` when the same hero follows itself because the
  others cannot act). When the fight ends the table's normal spotlight rule resumes.
* The owner cannot hand the spotlight over during a fight (`409 combat_turn_locked`); mode and
  timeout changes are allowed.

## Idle and absent players (bounded)

`POST /api/sessions/{id}/combat/skip` (JSON body, `{"spotlight_version": n}` optional) skips the
holder's turn: the hero takes the **Dodge action by default** (engine-resolved, same transaction and
telemetry as a typed turn, narrated as a normal round). Who may skip:

* the campaign owner, at any time;
* a seated player, once the holder is **overdue** (idle for the table's `timeout_seconds`, 300 s by
  default; never in owner-directed mode) or **absent**: the holder's controller is not on the EDG-047
  channel and `COMBAT_ABSENT_GRACE` (30 s) has passed.

Otherwise `409 combat_skip_not_due`. A spectator or outsider gets the usual 404. `GET /spotlight`
carries `combat: {round, holder_character_id, skip_due}` and `handover_due`. A skip is bounded to one
turn: the next turn waits its own timeout, so a returning player is never locked out of their next
slot. A hero whose seat is gone is passed over automatically (no wait).

## Channel events (EDG-047)

* `state.committed` carries the public combat state to the table. In a party fight it omits
  `pending_declaration` (a hero's own multi-part declaration waiting for yes/no stays with its player).
* `combat.turn_changed` (table): `{round, ended, outcome, current_turn, current_turn_character_id,
  skipped_character_id}`, after each committed combat turn.
* `turn.spotlight_changed` (table): the new holder and version, reason `combat_turn_passed` or
  `combat_turn_continues`.
* `character.state` (private): the acting player's sheet as before, and once per other hero whose
  hit points or death record the turn changed, that hero's own sheet to its controller only.

## Persistence and reload

All fight state is in `combat_states` (no migration): the rows keep each hero's data and the cursor
is `current_turn_index`. A reload mid-fight (`GET /sessions/{id}/combat`, the turn loader) rebuilds the
HUD, the holder and every hero's state. Hit points and death records of **every** hero the turn
changed are written in the turn's own transaction (`party_updates`), under row locks on those
assignments; a retried `turn_id` replays the stored receipt and writes nothing twice.

## Alternatives considered and rejected

* **Spotlight round robin during combat.** Rejected: the table's rotation is not initiative; the
  holder must come from the initiative order or foes would act on the wrong turn.
* **Resolve a whole round per request (all heroes' declarations at once).** Rejected: heroes decide on
  their own turn with the state the previous turn produced; simultaneous declarations cannot see each
  other's results and break reactions and death saves.
* **A new table for hero combat state.** Rejected: rows on the existing JSONB combatants already
  carry per-hero state, and the pause point fits `current_turn_index`; no migration, no second source of
  truth.
* **Foes retarget by lowest HP or by threat.** Rejected: predictable focus fire punishes whoever is
  hurt. One engine die over standing heroes is simple, auditable in the dice log and scripted in tests.
* **Autopilot for a repeatedly idle hero.** Rejected for now: it would let a table run a hero the
  player did not choose. A bounded skip per turn keeps the player in control of returning.
* **Waiting for a dying hero's input.** Rejected: a death save needs no decision, so it is automatic
  and the table never waits on a downed player.

## Known limits

Heroes have no positions or distances between each other (healing needs no reach); area spells hurt
only foes; Help is not simulated; a foe's multiattack may switch to another hero only when the first
target falls; a typed multi-part declaration needing confirmation can be left pending by a hero whose
turn it is, and is cleared by their next reply.
