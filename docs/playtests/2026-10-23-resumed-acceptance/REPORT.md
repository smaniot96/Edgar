# Resumed campaign acceptance — 23 October 2026

**The later AI chapter and rescue are now reachable, and the combat feedback fixes work in live play. Campaign acceptance remains incomplete: the rescued ferryman denies his authored identity, surrender produces no outcome, Wild Sheep cannot deliver its restoration ending, and all three preparation candidates remain held.**

This completes the interrupted campaign assessment before reviewing the separate combat-grid work. No product code, deployed image, historical messages or saved results were changed during this assessment. The grid work is outside this report's tested revision.

## Scope and provenance

Real Chrome against `http://127.0.0.1:8000/ui`, at **1440×1000 and 1280×800**, using the configured gameplay model **gpt-6-luna / low** and preparation profile **medium**. Deployed revision: **02487e92c056d9a142e4e51b1239bff13c209737**, API image `sha256:8658beea391c096634820855fb1d30f2154c6c8bbb3ec6db52333b4fb8f1d94d`. [Deployment receipt](deployment-verification.json).

Gameplay used the supported browser interface. Read-only public API snapshots verified saved state. Source/checkpoint diagnostics were inspected after the observed failures and are separated under `operator-diagnostics`; they did not supply subsequent secret routes or resolutions. No injected state, forced dice, save reset, resource refund or history rewrite was used.

The run contains **57 submissions, 55 eligible assessment actions and 54 normal latency samples**. Raw commands, replies and browser performance marks are in [turns.jsonl](turns.jsonl); numbered renders, public snapshots and screenshots retain each result. [Action audit](action-audit.json), [timing summary](timing-summary.json), [analysis script](analyze.py). Two operator mistakes are explicitly excluded below.

The comparison baseline is the [22 October preparation/runtime replay](../2026-10-22-preparation-runtime-replay/REPORT.md). The interrupted deployment/preparation cycle is retained in `../2026-10-22-preparation-runtime-acceptance-1520Z/`. The old written assessment remains available as comparison evidence.

## Campaign outcomes

| Route | Campaign / session / character | Final observed state |
| --- | --- | --- |
| Retained Silent Ferry exact-blocker recovery | **30 / 34 / 41**, Maple AI Format Replay L4 | Reached upper tollhouse room, rescued Jory, returned him to Mara and secured the barge with Athletics **24 vs DC20**. Scene **ch2_riverfront_resolution**, **36/36 HP**, features unspent. Jory denies being the ferryman; Mara still says the ferryman is missing. No coherent full ending. |
| Wild Sheep peaceful handover, then Noke combat | **32 / 36 / 42**, Iris Acceptance Peace L4 | Peaceful custody handover and escort worked. Noke defeated in **nine real rounds**, **20/36 HP**, Second Wind and Action Surge **0/1**. Victory persists on reload. Guz retains Finethir; restoration and finale remain unavailable. Compound-entry knowledge sample is excluded. |
| Independent Wild Sheep combat/surrender | **33 / 37 / 43**, Oak Acceptance Combat L4 | Publicly named half-orc hit for **8**, left **59/67 HP**. Two resolved rounds, both enemy attacks missed; hero **36/36 HP**, features unspent. Two confirmed concrete surrender offers gave no outcome. Escaped with sheep/scroll; **Fled** persists on reload. |

Wild Sheep uses the preserved sealed publication `wild_sheep_chase_chapter_source_fidelity_demonstration`, not either failed new PDF. The AI session retains `the_silent_ferry_preparation_replay_20261022`. Character level, equipment and resources remain independent between campaigns. Final state receipts: [AI](ai-final-public.json), [peace/Noke](peace-final-public.json), [combat/escape](combat-final-public.json).

**Noke arithmetic:** attacks dealt 11+12+6+6+5+10+9 = **59** damage. The first Ray of Frost hit for **22 cold**, followed by seven misses; round nine ended before Noke acted. Second Wind rolled/restored **6**, so **36−22+6=20 HP**. All eight enemy spell phases appear in the recorded events/history with actor, target and outcome. This verifies the exercised arithmetic and feedback, not encounter balance across repeated runs.

Turn 035 narrates the wand as held, but the persisted sheet still lists only chain mail, longsword and shield. It therefore does **not** establish inventory acquisition. The canonical wand mechanics and source ending remain unaccepted.

## Fixes accepted in this live sample

| Change | Evidence and practical limit |
| --- | --- |
| Preserve exact later-chapter scene IDs | New movement keeps **ch2_island_approach**, then **ch2_tollhouse_rescue**. Upper room/rescue/return are reachable; final scene is **ch2_riverfront_resolution**. The retained old pending turn initially completed against its old ch1 state; subsequent turns demonstrate the fix. |
| Combat spell history | Noke's one hit and seven misses are present in round events and visible encounter history. Incoming damage and HP agree. |
| Actor-specific attack dice and multiple attacks | Single hero attacks show their own die despite enemy disadvantage faces; Action Surge presents two attack cards. |
| Healing and feature ordering | Second Wind says **6 rolled · 6 HP restored** and occurs before the following attack. The declaration survives reload. No capped-healing case was exercised. |
| Resource and declaration safeguards | Exhausted features give clear feedback without spending a round or rolling. Danger warning precedes dice, survives reload and cancels safely. Negated attack text is safe; illegal double attacks are refused. |
| Target inspection | Inspecting Noke and adding a target to the draft does not change combat state. Before/after public snapshots are retained. |
| Withdrawal and terminal persistence | Disengage, pursuit and real escape work. Victory and Fled survive reload. |
| Map entities/routes | AI public map contains five physical place nodes, no NPC-as-place, and seven unique unordered discovered geographic routes. This accepts the sampled projection, not every prepared map. |
| Desktop and keyboard | Six geometry samples have no document horizontal overflow. Composer bottom is **968.39px / 768.39px** at the respective heights. Multiline draft survives reload; Escape closes Journal and returns focus to its button. |

[Geometry](desktop-layout.json), [keyboard/reload](keyboard-reload.json), [target inspection](before-target-inspection-public.json), [after target draft](after-target-draft-public.json), [reloaded victory](victory-reloaded-1280-public.json).

Loaded final maps were recaptured after readiness: [AI 1280](screenshots/ai-map-settled-1280.png), [AI 1440](screenshots/ai-map-settled-1440.png), [Wild Sheep 1280](screenshots/peace-map-settled-1280.png), [Wild Sheep 1440](screenshots/peace-map-settled-1440.png). The earlier `*-map-*` final captures showing loading/readiness errors are preserved as transient evidence and are not completed-render acceptance. The settled map is legible; static descriptions still need state reconciliation.

## Remaining defects and reproduction

P1 means an important workflow or story payoff is blocked; P2 means material loss of agency, consistency or feedback. Frequencies describe this sample only.

| Priority / owner | Reproduce and expected result | Observed result / frequency / evidence |
| --- | --- | --- |
| **P1 runtime identity and ending** | Rescue Jory from the tollhouse, ask about the missing ferryman, return him to Mara, secure the barge. Preserve authored identity and acknowledge the completed rescue. | Jory says **“I'm not the ferryman”**; Mara repeatedly says Jory is home but the ferryman is still missing, including after the successful barge check. Three final identity/resolution replies contradict authored identity: **041, 056, 057**. [041](041-render.txt), [056](056-render.txt), [057](057-render.txt). Post-play source audit identifies Jory as the ferryman in registry and guide; this is not missing rescue content. [Audit](operator-diagnostics/jory-identity-audit.json). |
| **P1 preparation repair** | Retry retained Wild Sheep checkpoint after the secret/chapter fix. Reuse paid work and either publish valid output or expose an actionable correction. | Secret join repair succeeds, then profile repair is rejected for failing to retain valid source pages/cite physical pages. Job **5d47435bacdd4af4b24a81d5f149bf2e**, retry_round1, continuity node, 16 artifacts. Held roughly25s after the Oct22 retry. No publication. Separate from model capability holds. |
| **P1 preparation recovery / P2 public progress** | Inspect fresh PDF and AI jobs after provider connection failure. Show retained work, distinct failure cause and safe recovery status. | Both are held after ambiguous dispatches without response IDs. UI gives generic **Preparation needs attention** and claims none/source_pdf retained even when more artifacts exist. No blind retry or duplicate paid dispatch was attempted. Details below. |
| **P1 legacy source ending** | After defeating Noke, acquire/use the wand with a willing wielder to restore Finethir and continue the finale. | Wand attunement, charges, True Polymorph and failure effects remain unsupported; no restoration, Bed Dragon or epilogue reached. The old sealed publication remains pinned. The failed fresh candidates do not establish a corrected publication. |
| **P2 surrender** | During independent half-orc combat, confirm return of the sheep and peaceful accompaniment; separately offer unconditional surrender of gear/sheep. Resolve terms/refusal and its consequences. | **2/2 confirmed offers** give the same generic “scene does not establish response / offer concrete exchange” non-outcome (**047, 051**). A third reasonable handover/ceasefire phrasing is rejected for lacking literal “offer surrender” and “move” (**048**). [047](047-render.txt), [048](048-render.txt), [051](051-render.txt). |
| **P2 custody-aware conversation** | Speak directly to physically present Finethir without taking him from Guz. Let him answer or state a concrete in-world obstruction. | **2/2 questions** refuse speech as “not here/out of reach” because he remains held, despite presence (**036, 037**). [036](036-render.txt), [037](037-render.txt). |
| **P2 item persistence** | Pick up fallen Noke's wand. Narration and inventory should agree about ownership. | **035** says it is in hand; persisted inventory does not contain it. [035 snapshot](035-public.json). No successful item acquisition claim is made. |
| **P2 state-aware map/journal** | Inspect entries after victory / after securing the barge. Describe current consequences consistently. | Noke entries retain active staging after his defeat; AI market description retains the drifting barge after it is secured. Static source staging competes with live state. Anonymous **Starting location** remains on Wild Sheep map. [Final public states](peace-final-public.json), [AI map render](ai-map-settled-1280-render.txt). |
| **P2 travel/viewpoint and intent** | Follow the track, survey exterior, call to Noke, ask that the door be opened. Respect arrival state, sightlines and declared action. | **010** narrates arrival while state remains trail; **011** reveals a bear inside the outhouse from the exterior. Exterior calls **012/014** produce no useful response. **015** turns a request to open the door into an unrequested thieves_tools check, **14 vs15**. [010](010-render.txt), [011](011-render.txt), [015](015-render.txt). |

The Jory source audit was performed only after final gameplay. No private ferry location was used to manufacture a successful ending. Earlier answers **038–041** had already exposed the contradiction; ordinary rescue, return and barge work still continued through public information.

## Preparation causes, kept separate

No new preparation submissions or paid retries were made in the resumed session. Read-only checkpoint inspection distinguishes retained paid work from public UI progress.

| Candidate | Last accepted work and hold | Result |
| --- | --- | --- |
| Retained PDF `the_wild_sheep_chase_preparation_replay_20261022` | Secret/chapter correction succeeded and retained structural/link work was reused. Profile correction failed: **“Retained structural repair was rejected: Profile page correction was rejected: Profile correction must retain valid source pages and cite existing physical pages.”** | Continuity hold, **16 artifacts**, retry1. This verifies repair progress, but not complete recovery or publication. |
| Fresh PDF `the_wild_sheep_chase_fresh_acceptance_20261022_1520z` | Six-page source/spine accepted; bundles node hit provider **APIConnectionError**. Task `2c63a2bdfead343db7f38718af50d1d4177131d8b46c3064c961cb81fde5f5e3` remains dispatching with no response ID. | Job **746a11f793e74445affe6aaaa4d3fe8a**, **6 artifacts**, held after approximately704s. Ambiguous dispatch; no safe successful retry demonstrated. |
| Fresh AI `the_crooked_ferry_acceptance_20261022_1520z` | Three chapter outputs accepted; fourth dispatch had **APIConnectionError**. Task `2c029647e9b4a19f0a14b273bcf2589ed23c20555d9f5d1aebd975ca18c5cec5` remains ambiguous. | Job **fba799f1208d49f082a57b13cafd7e37**, ai_chapter node, **22 artifacts**, held Oct22 18:31:33Z, approximately2h52m after submission. No publication or playable combat payload to accept. |

Public detail captures are the corresponding slug-named `*-public.json`, renders and screenshots in this folder. Exact checkpoint receipts are under `operator-diagnostics`. The fresh AI request actually submitted a roughly500-character brief with **Medium** size; the earlier draft's Small description is not authoritative. The original request receipt in the interrupted-cycle evidence records the submitted payload.

## Responsiveness and comparison

Times below are seconds, **first token p50/p90 → composer ready p50/p90**, measured in the browser. First visible text follows first token by about20–30ms. Quantiles use linear interpolation. Category counts are small and context-dependent.

| Source / action | n | First token | Composer ready |
| --- | ---: | ---: | ---: |
| Wild Sheep dialogue |9|11.31 /13.21|12.58 /14.69|
| Wild Sheep exploration |4|12.88 /15.67|14.47 /16.73|
| Wild Sheep travel |3|9.04 /12.73|10.11 /13.85|
| Wild Sheep scroll |2|14.01 /14.33|15.55 /15.93|
| Wild Sheep declaration |11|4.45 /5.48|4.68 /5.71|
| Wild Sheep resolved combat round |11|9.60 /12.40|10.61 /12.64|
| Wild Sheep surrender result |2|7.19 /8.83|7.39 /9.05|
| AI exploration |5|10.87 /13.46|12.41 /14.70|
| AI dialogue |2|13.00 /15.81|14.36 /16.98|
| AI rescue |1|10.20 /10.20|11.90 /11.90|
| AI resolution |2|11.70 /12.68|13.58 /14.34|

Across54 samples, acknowledgement/busy paint p50 is **28.75ms**, first token **9.43s**, and composer ready **10.86s** (p90 **15.70s**). Cancel took **0.87→1.06s**; escape **5.12→6.18s**. Journal-settled marks exist for51/54 samples. **No map-settled timing marks were recorded**, so map latency is unknown, not zero. Settled final map screenshots establish rendering only.

Turn006 stalled before the server received the POST because retained play tabs saturated Chrome's HTTP/1.1 connections. Closing obsolete play tabs released the request immediately. It remains in gameplay evidence and is excluded from ordinary model-latency statistics. [Recovery receipt](browser-tab-recovery.json). Earlier final map captures were similarly premature; settled recaptures avoid treating transient loading screens as successful map renders.

Exact-text baseline matches are retained in [timing-summary.json](timing-summary.json). Illustrative first-token/ready pairs, baseline → current:

- Peaceful offer: **9.00/10.61 →12.38/14.06s**.
- Follow Guz: **10.05/11.71 →13.65/14.78s**.
- Continue track: **10.39/12.92 →9.04/10.04s**.
- Exterior overview: **19.11/20.63 →10.52/12.55s**.

These share command text, not identical context, output length, dice or outcomes. **No general speedup is established.** Correctness improved in later-scene persistence and combat feedback; response cost still hurts when an answer offers no useful decision.

A conservative human audit marks **8/55 eligible actions (14.5%)** as adding no useful decision/progress: **007,012,014,036,037,039,047,051**. Baseline was14/80(17.5%), with different coverage. These are descriptive samples, not a controlled improvement rate. Repeated attacks advance real combat; confirmations, legitimate misses, cancellations and useful rules feedback are not automatically waste. [Repeated inputs](repeated-inputs.json), [per-action reasons](action-audit.json).

## Experience ratings and priorities

| Dimension | Score /5 | Assessment |
| --- | ---: | --- |
| Clarity |3|Combat rolls/history and feature feedback are clearer; vague custody refusals and static descriptions still mislead.|
| Agency |3|Rescue, movement, cancellation, combat and escape work. Concrete surrender and source restoration do not resolve.|
| Coherence |2|Saved later scenes, defeat and custody persist, but rescued Jory denies the identity that defines the rescue.|
| Rules feedback |4|The exercised Fighter/spell events, HP arithmetic, resource guards and ordered healing are reliable and readable; unsupported mechanics remain substantial.|
| Pacing |2|The rescue route now advances; repeated non-answers and blocked payoff still consume decisions.|
| Responsiveness |2|Acknowledgement is immediate; ordinary dialogue/exploration often costs12–17s to readiness.|
| Challenge |3|Real incoming damage and finite Fighter resources matter; one win and one escape do not establish balance.|
| Fun |3|A real rescue and legible tactical results are satisfying. Unacknowledged identity and unresolved endings undercut them.|

Prioritized follow-up:

1. Preserve canonical NPC identity and committed rescue/resolution facts across conversation, scene transitions and final acknowledgement; replay Jory's rescue through a coherent ending.
2. Complete retained PDF profile repair and safe ambiguous-dispatch recovery. Expose actual retained progress and distinct actionable causes. Publish/play fresh candidates before claiming preparation acceptance.
3. Make concrete surrender terms resolve to accepted terms, refusal with consequences, or an explicit supported alternative. Respect natural handover wording and custody-aware dialogue.
4. Reconcile narrated item ownership with inventory; support the required source ending or a valid authored alternative in a newly reviewed publication. Keep existing campaign pins intact.
5. Reconcile map/journal staging with current consequences, enforce earned sightlines and arrival state, and prevent undeclared skill checks.
6. After correctness, measure matched useful decisions and reduce long waits/non-answers. Evaluate grid tactics separately against the current revision.

## Preservation, exclusions and limits

Public inventory comparison: campaigns **19→20** (only new33; existing32 changed HP), characters **22→22** (only43 changed assignment), adventures **16→16**, all adventure summaries identical. **No missing IDs.** [Preservation comparison](preservation-comparison.json), [before](resume-inventory.json), [after](final-inventory.json). This verifies public preservation properties, not bytewise equality of every database row. Raw evidence and prior campaigns were retained.

**Operator exclusions:** turn016 named an unseen laboratory after the door attempt failed, using knowledge from an earlier baseline. It is excluded from earned-knowledge/clean compound-entry acceptance; subsequent resolved combat mechanics remain valid observations. Turn049 sent “yes” after the rejected declaration048 and had no pending action; it is excluded. Neither mistake is counted as a clean product progression or a product defect. Turn006 has the separate browser-timing exclusion above.

No executable product changes were made during the assessment, so the earlier deployment's reported **2,146 backend /479 frontend tests, build and lint** were not rerun for this report. Live browser acceptance is the new evidence here; it is not a claim that those suites prove the unresolved story workflows.

Untested/incomplete: no new PDF/AI publication could be played; no full nonviolent ending, restoration, Bed Dragon, wand-risk finale, reward/epilogue, fresh AI combat payload or capped-healing case was accepted. No half-orc victory/defeat continuation, multi-PC/grid tactics, other classes/levels, mobile, zoom or assistive technology was assessed. Source diagnostics identify causes but do not replace supported UI acceptance.
