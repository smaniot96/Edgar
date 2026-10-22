# Desktop implementation and retest

22 October 2026. Desktop scope only. Implementation branch: `codex/desktop-playtest-fixes` in `/Users/psmaniotto/.codex/worktrees/desktop-playtest-fixes/Edgar`.

The changes improve player agency, source interactions, setup and the desktop play workspace. The implementation was replayed against the actual local model and published adventure. Some broader acceptance goals remain open, especially canonical geography, complete later-adventure coverage and a demonstrated reduction in model waiting time.

## Delivered behavior

| Plan | Implementation | Evidence and limit |
| --- | --- | --- |
| D1 | Attacks require affirmative intent. Surrender is a separate confirmed nonviolent offer. Unsupported offers clarify without mutation; supported acceptance has a distinct `surrendered` outcome. | Live surrender produced an honest unsupported-evidence clarification without attack or flight. Supported acceptance/refusal covered deterministically; no claim of live accepted custody. |
| D2 | Generic source-object interaction context routes essential scroll usage through the prepared procedure. | Original short scroll phrase succeeded in multiple fresh campaigns; no sheep-specific phrase rule. |
| D3 | Narrator uses validated destination and arrival context; opening actor staging is conditional on current continuity. Explicit stationary exterior actions cannot establish an interior arrival. | Fresh route and stationary-call replay recorded below. Physical-place identity remains absent from the source schema. |
| D4 | Severe encounter warning appears before initiative or action dice, in the HUD and encounter view. | Level4 warning and cancel/reload verified; separate level1 fixture retains normal12HP. |
| D5 | Versioned persisted ordered execution plan drives confirmation and execution; excess attacks reject before dice. Ordered events and roll presentation include healing and attacks. | Pending heal-before-attack survived reload; mutation and ordering regression tests; fresh deployed result captures. |
| D6 | Routine dialogue requires a concrete reason for a check; failed hidden searches preserve public surroundings. | Finethir answered concrete questions; compound exploration describes buildings, doors and terrain. Repetition and source interruption still vary with the model. |
| D7 | Public actor descriptions include visible nonparticipants without exposing hidden identity/stats. Journal, header and map share safe public place labels. | Wolves/cloaked figure projected separately from active Guz roster. Unnamed nodes use “Unidentified place”; duplicate story-event nodes remain. |
| D8 | Supported presets can start at levels1–5 through real progression, with coherent HP/abilities and explicit unsupported-feature labels. Setup reuses the sole assigned hero. | Level4 Fighter created in UI; assignment/sheet/reload integration checks. New save explicitly preserves campaign HP/resources. |
| D9 | Desktop encounter schematic shows engagement, initiative, danger, participants and chronological events; inspecting/filling a target draft spends no action. | Schematic explicitly disclaims scale/terrain/cover; browser and frontend tests. No fabricated tactical grid. |
| D10 | Compact header/HUD, optional recap/ideas, tool side pane, keyboard/focus handling, visible confirmation controls and browser turn-stage measurements. | 1280×800 and1440×1000 captures; synthetic255-character title/long-warning fixtures labelled as previews. No matched25% speed improvement established. |

## Reproducibility and data preservation

First deployed revision: `5f1f742a94e745a03bf7ec96bfc1b11cd5e83fa5`. Intermediate deployed revision: `1e94bbed7245b12c36b8646a8b012aa4703dcfb0`. Continuity correction: `1c4fccbeb8f6709cfd2f93101dcd2e2623d5afee`. Final deployed source: `45a0f9e6a3ac6a2a1fccfa3053524deefdfaf81f`, image `sha256:93d33be65c91f55e85933fe903ab8007000bb32c03602f6e1b75a457002975d6`. Final rollback snapshot: `/Users/psmaniotto/EdgarBackups/desktop-retest-before-20261022-124236`. Image/source receipt: `/Users/psmaniotto/EdgarBackups/desktop-fixes-images-45a0f9e6/images.json`. The local API was replaced independently of PostgreSQL, Redis and Qdrant. Their container identities/mounts and the API data mounts were checked. No migration or source republication was performed.

First rollback snapshot: `/Users/psmaniotto/EdgarBackups/desktop-retest-before-20261022-120847` (PostgreSQL dump, APFS source-data clone, prior image override). Image/source receipts live under `/Users/psmaniotto/EdgarBackups/desktop-fixes-images-*`.

Configured live model: `gpt-6-luna`, low reasoning; profile configuration version3. Active source collection: `wild_sheep_chase_chapter_source_fidelity_demonstration`; published version `b30e8f2b0c82618228308c27f2448b4a4e6e3ffe1affaf108d15366caae95d35`.

| Campaign / session / hero | Purpose |
| --- | --- |
|18 /21 /28|Fresh level4 combat and setup replay, Fighter36HP|
|19 /22 /29|Fresh level4 noncombat travel replay|
|20 /23 /30|Lantern Under Bridge second-adventure dialogue/exploration smoke, level1|
|21 /24 /31|Fresh level1 severe-danger verification, Fighter12HP|
|22 /25 /32|Fresh follow-up travel verification, Fighter36HP|
|23 /26 /33|Fresh continuity verification, Fighter36HP|
|23 /27 /33|Fresh save in campaign23 for final transition replay; same hero and retained campaign resources|

Campaign17, sessions19/20 and hero27 were preserved as original evidence. The original `dev` checkout and its pre-existing file deletions were left untouched. Test fixtures were created through supported APIs/UI; no stored turn/world history was rewritten to manufacture a pass.

## Verification

- Frontend:474 tests across74 files passed; production build and lint passed.
- Combat/prompt/history focused suite:510 passed before the final small review correction; subsequent focused results recorded below.
- API stream contract plus starting-level integration:41 passed,1 skipped. Separate real-character projection checks covered `milestones_pending`0/1 and frontend stream consumption.
- Broad API run:837 passed,6 failed,6 skipped,9 setup errors. The same failures/errors were reproduced on the clean baseline: campaign-source tests2, route classification1, story-turn effects2, simple-route pin1, and9 missing-`preparation_commission` setup errors. They are not reported as new regressions.
- Broad agent/core run before follow-up fixes:1570 passed,6 failed. Five were independently reproduced on baseline (3 resolution-policy checks, failure-discovery/objective-clock, absent-promise hash). The readied-reaction duplicate-dice failure was corrected and covered by the focused combat suite.
- Whole-tree Ruff and `git diff --check` passed at the source review checkpoints. Final integrated results are recorded below.

The test totals above describe separate, overlapping suites and must not be added together. Baseline failure logs are retained in `/tmp/edgar-api-baseline.log`, `/tmp/edgar-api-final.log`, `/tmp/edgar-agent-final.log` for this local session.

## Live evidence and assessment

Raw inputs, full rendered output, browser measures and screenshots are in [turns.jsonl](turns.jsonl), [screenshots](screenshots/) and [browser-timing-summary.json](browser-timing-summary.json). Public session/journal/map snapshots accompany the affected turns.

The short scroll wording now opens the adventure reliably in the fresh replays. Concrete questions about Finethir, Noke, the wand and the route produced usable answers. Combat declarations are easier to trust: the danger is visible before committing, cancellation does not spend a turn, double attacks are refused, and healing/attack order survives confirmation and reload. The encounter view makes Guz and the publicly visible companions understandable without implying that every described creature participates in this simplified fight.

Travel now has meaningful stops and exploration supplies usable public detail without inventing an arbitrary failed perception roll. That improves the choice of approach. The unresolved event/place distinction still harms orientation: several nodes can represent changes in the same physical area, and safe unnamed labels are less helpful than a real earned place identity.

Responsiveness remains dominated by model generation. The browser gives quick local busy feedback, but narrative waits still interrupt the pace. The timing dataset mixes confirmations, combat and story actions; it is not a matched before/after experiment. A double-requestAnimationFrame measurement is a paint opportunity, not a physical screen visibility sensor. Headers are not narrative text. No claim of a25% latency improvement is made.

### Evidence caveats

The early harness returned too soon during the phase change for rows07/08. Their shell elapsed times and incomplete screenshots are not completion evidence; later cumulative Performance measures and completed snapshots recover the actual turn timing. Captures04 and37-opening show loading, not accepted completed output. Some early CDP captures were1440×913 rather than1440×1000; final captures explicitly set the viewport. Files prefixed `preview-` use synthetic warning/title fixtures and are not deployed gameplay acceptance.

## Remaining problems and scope limits

1. **P2 — Campaign map still models event nodes as separate places.** Follow aftermath→trail→compound and open Campaign Map. Several anonymous nodes can remain; switching to safe public labels prevents prose fragments/private titles but does not merge physical locations. Fix requires a physical-place identity distinct from scene/event identity, plus source compilation and persisted-projection migration.
2. **P2 — Some nonviolent dialogue repeats demands instead of answering.** In session22, ask Guz to let you accompany Finethir to Noke without fighting (turns28–29). The model can repeat his demand or departure rather than acknowledge the specific proposal. Guarding unnecessary checks does not guarantee useful source-grounded dialogue.
3. **P2 — Model waiting still interrupts pacing.** Submit a normal source-grounded story turn such as reading the scroll or exploring the compound. First narrative text can take many seconds; see measured distributions. Optimize the measured planner/context path with matched correctness/latency replays before claiming a speed improvement.
4. **Untested acceptance:** normal combat victory, a live source-supported accepted surrender, later Noke encounters and the finale were not fully completed. Deterministic tests do not establish live language behavior or player enjoyment for those branches.

Further live regressions, fixes and final build evidence follow below.

## Final source review and deployed replay

Follow-up source revision: `1e94bbed7245b12c36b8646a8b012aa4703dcfb0`.

Independent review corrected two general edge cases before this build: a verified absence statement such as “Three large wolves are not present” cannot create a visible participant, and an indoor scene whose prose does not begin “Inside” cannot be treated as proof of an exterior viewpoint. The final integrated combat, history, visibility, story-turn, observation, public-place, journal and prompt-registry suite passed **196 tests**. Whole-tree Ruff and diff whitespace checks passed.

The hidden-occupant disclosure was traced to accepted observations in event449, before narration: a chapter-content span stated the occupant, while current-event guidance explicitly said it was not automatically visible. A bounded validator now carries that explicit source visibility qualifier across duplicate spans from the same source/manifest. It preserves source-supported sensory procedures and successful engine-backed searches. This is a targeted boundary correction, not a general secrecy classifier. [Diagnostic receipt](37-progression-followup-evidence.json).

Deployed checks on revision `1e94bbed`:

- **Danger/desktop controls passed:** session24, `I attack Guz with my longsword to protect Finethir.` at1280×800 produced `far_above_deadly`, retained12/12 and67/67HP and empty initiative. Confirm/Cancel occupied y658–694 and Send ended at y768. The pending declaration survived reload; `no` cleared it without a round or HP change. See [danger](screenshots/45-final-level1-danger.png), [encounter view](screenshots/46-final-level1-encounter.png), [reload](screenshots/47-final-level1-pending-reloaded.png) and [state](48-final-level1-combat.json). Public participants include the three large wolves and huge cloaked figure without hidden identity or statistics.
- **Intermediate D3 continuity failure:** session25 handover now gives coherent immediate narration with Guz taking Finethir away, but the next follow action again states that the white sheep remains behind by the scroll. Reproduce: use exact inputs in turns49/50 of [raw replay](turns.jsonl). [Immediate handover](screenshots/49-final-handover.png); [next-turn contradiction](screenshots/50-final-follow.png). Conditional arrival instructions alone do not establish persistent actor custody/position. This was a P1 in that intermediate build. The subsequent correction and its fresh replay are documented below; these captures preserve the original failure.

Additional deployed evidence on `1e94bbed`:

- **Stationary call passed:** session25 reached the compound, then used the original `I stay in plain view and call out...` input. Scene remained `chapter_two_compound_arrival`; Noke answered from inside while the hero stayed on the lawn. [Screenshot](screenshots/54-final-stationary-call.png), [before/after canonical state, map, journal and messages](54-final-stationary-state.json).
- **Visible exploration passed for the hidden occupant case:** turn53 described the platforms, entrances, apes, huts and outhouse without disclosing the unseen bear. [Screenshot](screenshots/53-final-visible-exploration.png). This does not establish a general hidden-information guarantee: the subsequent answer also mentions Noke working on spells inside, an interior detail the hero cannot directly see.
- **Withdrawal chronology passed:** session21 used `I disengage, then withdraw from Guz into ranged distance.` and confirmed. The DM stated that withdrawal succeeds, then Guz closes and attacks on his turn; the event list agrees. [Screenshot](screenshots/59-final-disengage-encounter.png), [public result](58-final-combat-public.json). Hero went22→11HP from that ordinary enemy attack.
- **Ordered roll display passed:** reloaded saved healing round renders “Second Wind …6 healing” before the attack. [Screenshot](screenshots/61-final-heal-before-attack-card.png), [rendered text](59-final-heal-card-order.json).
- **Target inspection passed:** inspecting Guz and selecting Add target to draft filled `Guz`; public combat state was byte-equivalent before/after. [Screenshot](screenshots/60-final-target-draft.png), [receipt](60-final-target-draft.json).
- **Second-adventure travel passed:** in Lantern Under Bridge, `I walk downstream along the bank, following the blue glimmer toward the old ferry shelter.` reached the shelter and described the child, lantern and next choices. [Screenshot](screenshots/62-final-other-adventure-travel.png), [state](62-final-other-adventure-state.json).

### Final continuity correction

The residual handover failure was traced to adjudication489: the accepted move's `player_text` copied static arrival staging and the narrator promoted it to live validated consequences, despite no accepted observations. The final correction removes static module move/visit prose from current and historical live consequence blocks while retaining position/status/provenance. It includes that prose once as conditional reference context and gives newest budgeted DM continuity precedence for prior actor departures/custody. Persisted source/history receipts remain untouched. [Exact route](55-arrival-continuity-receipt.json).

A cross-adventure regression discovered during this review was also corrected: already validated homebrew place identities bypass private prepared-scene label heuristics. The existing ten-turn homebrew test again retains “Ossuary Crypt,” without adding a special place-name word rule.

After these final changes, **83 desktop story, homebrew, token-budget, history, prompt-registry and journal checks passed**, plus whole-tree Ruff and diff whitespace checks. This suite overlaps earlier totals. The continuity instructions stay within existing fitted history; no duplicate prior narration bypasses the token budget.

### Intermediate travel finding and remaining information problem

**Intermediate P2 — Repeated explicit travel could consume a turn without changing the decision.** In fresh session25, after handing Finethir over and following Guz, send `I continue along the well-trodden track through the tall oaks until I reach Noke’s home.` twice. Turns51/52 repeat that the track continues and the home has not been reached, with no concrete obstacle. Then `I look around Noke’s home. Describe the buildings, doors, terrain, and visible inhabitants so I can decide how to approach.` advances to the compound. Expected: an explicit continued journey should advance to the next supported stop or explain a specific intervening obstacle. The old failure of narrated arrival without canonical movement is guarded; the model can still withhold a permitted transition. [Repeated track turn](screenshots/52-final-reach-compound.png), [following exploration](screenshots/53-final-visible-exploration.png).

**P2 — Interior activity can still enter exterior narration.** Reach the compound, remain on the lawn and call to Noke (session25 turn54). The answer correctly keeps the player outside, but adds that Noke is “working furiously on new spells” inside. Expected: exterior narration should convey audible speech and visible details, or qualify an inference; it should not assert an unseen interior activity. The explicit-visibility observation correction protects the demonstrated hidden occupant span, not every possible private source detail. [Reproduction](screenshots/54-final-stationary-call.png).

**Campaign-map orientation reproduction:** after reaching the compound in session25, open Map → Campaign map and inspect visited nodes. The source represents chapter events and physical places with one scene identity, so multiple event nodes remain distinct even when they describe the same area. Labels now avoid prose fragments/private titles, but several can read “Unidentified place.” Expected: one persistent physical location with meaningful publicly earned identity and event history attached to it.


### Bounded transition repair and final deployment

The fresh continuity replay on `1c4fccbe` preserved Finethir's handover across the next follow turn. However, repeated travel still stalled at source-supported transitions. Exact adjudication receipts exposed two avoidable causes: a proposed `visit` to a different linked scene was rejected where the engine expected `move`, and an extra historical play-event citation invalidated a proposal that also contained valid current reviewed source proof.

Revision `45a0f9e6a3ac6a2a1fccfa3053524deefdfaf81f` normalizes `visit` only when the destination is already linked and permitted. All source, trigger, one-hop, stationary-position and commit-time validation remains active. It removes an extra historical origin only when that exact origin belongs to a committed same-source/version fact and valid current reviewed source evidence remains. Historical evidence alone, unknown references, foreign versions, missing triggers and gated destinations still reject. No absent move is invented. A related regression ensures current-scene public evidence can justify departure but cannot become destination arrival prose in the map or journal.

Final integrated checks covered desktop story turns, story effects, history, token budget, combat visibility, homebrew consequences, public places, journal and prompt registry: **141 passed; 2 failures reproduced earlier on unchanged baseline** (`test_explicit_failure_discovery_and_monotonic_objective_clock`, `test_absent_promise_anchor_preserves_existing_effect_hash`). Whole-tree Ruff and whitespace checks passed. The focused transition suite also checks arrival preloading, unchanged raw planner receipts, current visits, unlinked/gated destinations, missing trigger/source proof, stationary entry, mixed historical/current evidence, historical-only evidence, foreign manifests and invalid knowledge IDs.

Focused progression checks excluding those established baseline failures: **104 passed, 2 deselected**, including earned-answer regressions. The final local API rollout again verified unchanged PostgreSQL/Redis/Qdrant identities and mounts, plus unchanged API data mounts.


### Fresh final replay on `45a0f9e6`

Save27 begins after the scroll, Finethir's directions and the pursuers' arrival, under test campaign23. It uses the same level4 hero33 as save26; campaign resources remain intact. After deploying the final source:

| Input / capture | Saved result | Observed behavior |
| --- | --- | --- |
| [Handover](screenshots/76-final-transition-handover.png) | `chapter_one_aftermath` | Finethir enters Guz's custody; group departs. |
| [Openly follow Guz](screenshots/77-final-transition-follow.png) | `chapter_two_trail` | Linked travel advances to the track without the previous repeated rejection. No sheep reappears behind the group. |
| [Continue along the track](screenshots/78-final-transition-compound.png) | `chapter_two_compound_arrival` | A single continue advances to the compound, describing the house, platforms, apes, huts and outhouse. The unseen bear is not disclosed. |
| [Stationary call](screenshots/79-final-transition-stationary-call.png) | Remains `chapter_two_compound_arrival` | Player remains outside. The answer still asserts that Noke is working inside and suggests calling again despite the just-completed call. |
| [Campaign map](screenshots/80-final-campaign-map.png) | Same compound state | Five event nodes and four routes render; labels are all “Unidentified place,” confirming the remaining orientation problem. |

The corresponding `76`–`79-final-transition-state.json` files contain the exact public session, messages, map and journal snapshots. Raw inputs and outputs are retained in `turns.jsonl`. These results support the two bounded transition repairs and the demonstrated custody correction; they do not establish general story completion. The handover answer also compresses the journey prematurely with “the approach to Noke's home” while the saved state is still aftermath. The next answer correctly establishes the trail, but that wording can confuse distance and progress.

**Remaining P2, journal/map detail continuity:** at the final compound, inspect the earlier aftermath node in the map or journal. Its stored source description says the sheep stays close even though the selected branch handed Finethir to Guz. The narrator no longer promotes that static arrival prose into live continuity, but the historical place detail still displays its unqualified default staging. Expected: place descriptions should retain physical facts and qualify or omit branch-dependent actor staging. This requires a further projection change; source/history receipts should remain auditable. See [final public map/journal snapshot](78-final-transition-state.json).

**Remaining P2, stationary dialogue response:** use the exact call in capture79. It does not move the player indoors, but reveals “Noke is working inside the central platform” without an established sightline and ends by suggesting calling him again. Expected: give an audible response, an observable lack of response, or a qualified inference, then offer a new decision. This reproduces the earlier private-interior and repetitive-dialogue limitations on the final deployed source.

Final timing sample: **48 complete measured turns**, mixed story/combat/confirmation categories. Median busy paint opportunity **30.3ms**, headers **308.7ms**, first narrative token **9,333.7ms**, first text paint opportunity **9,363.8ms**, composer ready **10,929.3ms**. The first-token range was0.87–14.50 seconds and ready range1.21–17.16 seconds. The UI acknowledges input quickly; model waiting remains the main pacing cost. No matched baseline speedup is claimed.

Code commits are `5f1f742`, `1e94bbe`, `1c4fccb` and `45a0f9e6`; the last is the running source. A subsequent evidence-only commit contains this report and captures. No merge or push was performed.
