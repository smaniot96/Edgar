# Campaign preparation pipeline review — 2026-10-20

Review basis: development HEAD `fbc6162` plus the existing uncommitted route/title/preflight changes; held runtime HEAD `46a5170`; retained artifacts for `prep-95ea542c81af43ec9dea3f61a77e9f85`, adventure `lost_mine_of_phandelver_edg_055_rerun_3`. Line references below describe that reviewed state and may move during remediation.

The review itself was read-only: no edits, tests, provider or embedding calls, runtime restart, or campaign continuation. This report was saved afterward when implementation was authorized. The current run must remain held until local representation, progression, grounding, and validation defects are repaired. The six completed requests produced substantial reusable content, but the saved blueprint is not yet a demonstrably playable campaign. Removing route-count and title-length failures alone exposes further structural failures.

The existing foundations should remain: immutable source/binding identities, exact offsets, full PDF retention, separate public/private evidence, durable provider receipts, retained proposals/corrections, independent critique, and publication seals. The central defect is the boundary between semantic authoring and executable campaign state, compounded by repair and accounting behavior.

## Prioritized findings

### P1 — Executable advancement is missing from the retained blueprint

The latest retained proposal contains eight objectives, fifteen milestones, twenty-nine clues, seven hazards, and no endings. Its forty-one effects comprise twenty discovery effects, ten environment effects, six clock effects, three relationship effects, and two milestone effects targeting the same milestone. There are no objective effects; fourteen of fifteen milestones have no authored emitter.

Thirteen gated route occurrences, involving twelve distinct facts, lack any authored effect producing their prerequisite. `work_secret` gates two routes. Other missing producers include Carp's tunnel directions, Thundertree leads, several castle/cave directions, and the concealed canvas route. Eight unproduced discoverable facts also have no accessible setup.

`apps/api/services/preparation_blueprint.py:439–447` gates a discoverable fact's reveal behind its own discovery flag; explicit producers come from declared decision effects at `:471–532`. Definition status defaults at `:590–593` do not create transitions. Generic runtime adjudication can propose some source-backed effects, so absence of an authored producer does not prove every such fact is impossible in play. It does leave mandatory advancement dependent on improvisation outside the preparation proof. Facts with neither setup nor producer are particularly problematic because runtime candidates come from accessible scene passages and attached entities (`apps/agent/src/agent/nodes/story_effects.py:59–141`; `core/src/edgar_core/manifests.py:792–806`).

The compact schema requires every approach to have a check, DC, success, and failure (`preparation_blueprint.py:80–96`), and compilation hardcodes `mode="check"` (`:514–517`). This cannot faithfully express reading an observable inscription, receiving promised directions, confirming a rescue, or advancing after a real victory. Canonical/runtime policies already support automatic mode (`core/src/edgar_core/manifests.py:264–296`). Prepared branches independently require success and failure/partial outcomes (`core/src/edgar_core/prepared.py:494–499`), so automatic actions must not be forced into check branches.

Add explicit automatic/action/event transitions, separate from genuine decisions. Source-backed observations and handovers should produce intended facts without invented DCs. Combat-dependent transitions must consume real engine outcomes linked to canonical actors/encounters. Entering a boss scene cannot imply defeating its boss. Current combat outcome identities are hashes of combat/combatant rather than canonical campaign actor identities (`apps/api/services/story_state.py:207–260`); establish that linkage before a transition consumes a canonical defeat fact.

### P1 — Path validation does not prove campaign completion

The path explorer searches for a witness for each authored outcome, uses `expected = set(outcomes)`, and stops when all those outcomes have witnesses (`core/src/edgar_core/coherence.py:767–774`, `:932–935`, `:1158–1174`). It does not require objective completion, milestone advancement, or a terminal campaign ending. Omitted advancement can therefore escape validation.

Prepared validation checks objective/milestone ownership (`core/src/edgar_core/prepared.py:309–314`) and checks declared endings only when endings exist (`:500–503`). It does not require a campaign ending or prove objective completion. Chapter checks reject duplicate advances/climaxes but do not implement the nearby comment's requirement for a new advance (`core/src/edgar_core/coherence.py:493–530`).

Taking any branch outcome currently marks its whole quest resolved, preventing subsequent decisions in the same quest (`coherence.py:996–999`, `:1134`). Missing intermediate transitions cannot be added correctly without changing that assumption.

Require a valid emitter for every required objective/milestone status and gate; a causal gate witness without circular self-disclosure; at least one coherent success path completing mandatory achievements and a terminal ending; and separate witnesses for meaningful supported failures/withdrawals/alternative endings. Do not require mutually exclusive endings on one path. Preserve authored-outcome coverage as an additional requirement. Guards, disclosure, custody, consumption, and event prerequisites must match runtime. Resolve quests only on terminal quest conditions.

P10 currently doubles state/depth budgets and reruns from scratch (`apps/api/services/preparation_stages.py:951–978`). Larger searches cannot repair missing goals or incorrect semantics. Run a producer/dependency inventory before exploration or paid review. Do not weaken thresholds.

### P1 — Alternative routes cannot be represented faithfully

Three saved `(from_scene, to_scene)` pairs occur more than once: `sc0401→sc0405` has two alternative Thundertree prerequisites; `sc0410→sc0501` has four castle leads; `sc0410→sc0601` has four cave leads. Full IDs have prefix `phv10c3095b66`.

The compiler emits every alternative as another link/route (`preparation_blueprint.py:557–570`). Prepared validation uses only `(from,to)` as identity, rejects duplicates, and later dictionaries overwrite alternatives (`core/src/edgar_core/prepared.py:288–299`, `:316–328`). Runtime conditions are an AND over one dictionary (`core/src/edgar_core/manifests.py:789–805`). Dropping duplicate routes loses valid approaches; merging requirements creates unintended AND semantics; removing gates exposes unearned destinations.

For genuinely equivalent directions, the smallest coherent fix is one earned-location fact per destination. Each legitimate directions transition emits both its individual source clue and that common fact; one route requires the common fact. Preserve source evidence and conditions. Genuinely different physical routes need explicit alternatives with consistent OR semantics across runtime, prepared validation, maps, and exploration.

Also ensure secret-route reveal and traversal agree. Runtime reads `Link.requires`, not prepared `reveal_requires`. Current saved private routes match their gates, but the compiler should enforce the intended implication.

### P1 — Paid repairs include compiler and source infrastructure failures

Compact P04 authors, compiles, then grounds each act before acceptance (`apps/api/services/preparation_stages.py:736–770`). `creature_unresolved` is converted to `stage_evidence_invalid` by the generic exception allowlist (`:1053–1073`), and a known-usage `ValueError` can trigger another paid semantic correction (`:545–568`). The first failed act hides later errors. Exact-creature errors also discard the creature name and detailed cause (`apps/api/services/preparation_grounding.py:295–301`).

Separate compiler/representation defects, unavailable/ambiguous sources, unsupported engine mechanics, provider/quota failures, and actual semantic deficiencies. Preserve the proposal at each boundary. Collect all local compile, source, actor, encounter, disclosure, route, producer, and campaign-coverage findings before another paid request. Formatting, IDs, deterministic schema translation, and source-index repair belong in code.

### P1 — Accounting discards known usage; quota failures are misclassified

Seven durable responses show six completed requests with 637,528 input tokens, 76,670 output tokens, 714,198 total tokens, and $4.083512 estimated at existing 4/20 micro-USD token rates. The seventh failed with `credit_balance_exhausted` and no usage. These are code-rate estimates, not a claim about invoiced charges.

The checkpoint reports one spent call, 224,000 tokens, $2.432, and `provider_output_invalid`. `_usage_sum` marks the aggregate unknown when any subcall lacks usage (`preparation_stages.py:128–147`); `_charged` then substitutes the whole task reservation instead of retaining the six known responses (`apps/api/services/preparation.py:1073–1082`). Failure recording uses that aggregate (`:885–939`). The provider adapter attempts JSON parsing and labels unsuccessful Responses invalid output without first handling `response.error` (`apps/api/services/preparation_provider.py:192–253`).

Reconcile from unique response IDs. Keep known charges separate from unresolved estimates. Unknown later usage must not erase known earlier usage. Exhausted credits should pause as quota failure while retaining the semantic proposal. Assert zero usage only when explicit provider rejection proves no inference. Distinguish estimated price from invoiced charge and do not invent a failed-response charge. Parent already synchronized terminal metadata; the safe-error taxonomy still needs correction.

### P1 — Exact source readiness and actor binding are incomplete

Saved MM evidence contains missing exact sections, mixed stat-block boundaries, a `Flame Skull`/`Flameskull` discrepancy, and a bullet before Skeleton's `Armor Class` that strict record-start parsing rejects (`db/vector/creatures.py:18–20`, `:48–52`). This is artifact evidence, not a live database diagnosis; no API lookup was performed during review. Keep exact source identity and repair verified boundaries/catalog entries locally. Never invent stats or substitute another monster.

Blueprint actor titles include motives, secrets, and explanatory prose; the compiler uses them as entity titles (`preparation_blueprint.py:439–440`). Runtime exact actor matching (`db/vector/creatures.py:114–139`) will not match `Nezznar` to `Nezznar: source-specific drow wizard, curious manipulator...`. Important actors including Nezznar, Iarno, Sildar, Kost, Reidoth, and Mormesk lack explicit stat references. Use short canonical names, reviewed aliases where needed, and separate private descriptions. Do not fix this with fuzzy substring matching.

Canonical references/resolver support module actor records (`db/vector/creatures.py:171–193`), but compact compilation only emits MM template bindings (`preparation_blueprint.py:450–454`). Add explicit source-specific module bindings and preflight all potentially used actors, not only encounter roster names. Supply the author with a verified available reference catalog; current evidence omits it (`preparation_blueprint.py:136–149`).

### P2 — Encounter composition and progression need an explicit full-campaign model

Every encounter is compiled at the initial hero level (`preparation_blueprint.py:605–613`), while grounding rejects authored level changes (`apps/api/services/preparation_authoring.py:203–205`). Prepared-play binding rejects a changed base hero snapshot (`apps/api/services/prepared_play.py:55–69`). Session progression may use separate state, so this does not prove every runtime level-up fails; preparation nevertheless lacks a coherent declared adaptation path. Earned progression should update mechanics without full paid reauthoring or invalidating source identity.

Some saved scene rosters aggregate populations from separate rooms. Solo truncation can then remove significant opponents or misrepresent simultaneous participation. Preserve source room/spawn groups and distinguish participants, reinforcements, and avoidable threats. Blueprint difficulty defaults to `deadly`; categorical comparison cannot distinguish just-deadly from dramatically over-deadly (`preparation_blueprint.py:35–37`; `preparation_grounding.py:448–458`). Preserve dangerous source encounters but assess mandatory fights, actual participants, avoidance, and resource implications meaningfully.

### P2 — Authoring and correction contracts create avoidable failures

Blueprint effects accept arbitrary statuses and loosely applicable fields (`preparation_blueprint.py:70–89`) while canonical effects constrain them. The instruction says clock/consume/environment costs need a declared operation (`:187–190`), while canonical validation allows operation only for use/environment (`core/src/edgar_core/manifests.py:251–260`). Use typed variants and supported check/status vocabularies.

Correction accepts the initial authoring instruction but replaces it with generic patch rules; it even instructs compact blueprints to split quotes/preserve passage IDs despite integer source-span references (`apps/api/services/preparation_corrections.py:342–367`). Use the same authoritative schema-specific contract for initial generation and patches. String diagnostics are truncated at 12,000 characters (`preparation_proposals.py:57–63`), and source narrowing falls back to the entire PDF if affected records/spans cannot be identified (`preparation_corrections.py:121–156`). Provide structured paths, IDs, source spans, and classification instead.

### P2 — Fresh-PDF efficiency and user-facing limits need separate remediation

This is not a fresh end-to-end benchmark: P01–P03 were imported. The first P04 response took approximately 16.9 minutes and emitted 50,321 output tokens including 9,168 reasoning tokens. Six completed requests span approximately 28.6 minutes and already estimate $4.08 before completion/review.

Fresh P02 already avoids legacy per-page/source-study calls for PDFs below the measured threshold and groups oversized sources (`preparation_stages.py:271–294`, `:321–337`). Do not attribute the legacy eighteen-study behavior to the current fresh path. Compact P04 remains opt-in: fresh PDF commission creation does not select it automatically (`preparation_runtime.py:242–258`), and ordinary planning creates one authoring task per act (`preparation_stages.py:351–367`).

Reduce output through representation: omit irrelevant/default wire fields, use concise typed records, retain source prose once, reference stable evidence IDs, separate names from private descriptions, and compile deterministic joins/mechanics. Do not remove necessary executable transitions. Compact P09 currently sends all source chapters, blueprint, and substantial canonical evidence in one request without legacy partition fallback (`preparation_blueprint.py:638–665`). Measure and partition for actual provider context capacity while preserving complete independent review and stable pointers.

The UI says `Maximum spend` and `Preparation stops at this budget` (`frontend/src/routes/home/PreparationFields.tsx:54–62`) while server spend is advisory. Supervisor attempt gates still stop tasks (`preparation.py:621–622`, `:932–934`; retry selection `:340–350`; runtime retry selection `preparation_runtime.py:129–135`). Remove limit-based stopping in favor of estimates, durable progress, no-progress detection, and real external failures. Do not introduce token/call/cost/attempt/repair/time caps.

## Implementation and recovery order

1. Preserve the held run/snapshot, proposals, patches, receipts, accepted design, studies, original PDF/bindings, and baseline. Reconcile known usage from unique Responses IDs; distinguish the explicit exhausted-credit rejection from unknown inference outcomes. Keep the same run identity.
2. Repair deterministic infrastructure first: routes/titles, alternative route semantics, actor naming/private descriptions/stat references, verified creature readiness, and distinct failure categories. Collect exhaustive structured diagnostics.
3. Add automatic/action/event transitions and matching validation together. Link real engine outcomes to canonical campaign identities. Resolve quests on terminal conditions. Define mandatory achievements/endings and prove them with runtime-equivalent semantics.
4. Before any paid dispatch, locally preflight the retained blueprint across compile/schema, source/actor/encounter grounding, disclosure, route, producer, and campaign-goal coverage. Fix representation/source defects locally and leave a precise list of remaining semantic decisions.
5. Reuse the six-request blueprint for one planned focused semantic completion covering missing producers, source-faithful advances, ending conditions, and identified encounter/progression decisions. Preserve unaffected IDs, evidence, prose, and valid choices. This is a planning target, not a call cap; further calls require concrete remaining semantic defects. Do not redo source study/design.
6. Require local proof before independent paid review. Then retain P09, P10, P12 checks/seals after any review repair. Critic approval cannot replace executable reachability.
7. Resume the same saved run after checks are sound. Reuse accepted artifacts and receipt-backed retained proposals; do not recharge/redispatch completed responses. The retained schema-valid reuse path (`preparation_corrections.py:287–290`) still needs full repaired compiler/preflight acceptance.

After recovery, separately benchmark the complete fresh-PDF path from upload to publication. Select the intended compact pipeline for new commissions, reduce semantic serialization/repeated evidence, and partition oversized review context. Report stage wall time, unique response IDs, input/output/reasoning tokens, known estimated charges, local processing, and unresolved usage. Approximately twenty minutes and five dollars are optimization targets, with correctness and preserved paid work intact.

## Exact creature source remediation and read-only grounding inventory

Implementation in the development worktree now separates ordinary active-index records, supplemental exact original-PDF records, and private prepared-module passages. `preparation_grounding.py` exposes `preflight_creature_sources`, collecting the requested exact creature catalog and every missing/unreadable source before a model correction. Encounter helpers also accept `actor_manifest` and verified `source_pages`, resolving a request's explicit `actor_id` through its exact stat reference, actor/scene association, reviewed passage, physical page, original offsets and text digest. Module receipts retain `index_version=null`, `evidence_origin=prepared_module` and their exact passage proof. A name mismatch, unreviewed/excluded passage, changed source span, or unrelated template binding remains a real diagnostic. No fuzzy actor or monster substitution was introduced.

The read-only live MM inventory supersedes the earlier artifact-only diagnosis. The active source is `D&D 5E - Monster Manual`, edition 2014, PDF SHA `51cde09adade4186028088798c9d5bc4bf2b63e1b274080d9a975943b79069f5`, extraction SHA `46dc6b2757140152d51c9df271ccc283cfcd9a61c3ed2a9b361f949d2cfe4e83`, index version `9672493ba438e96ab4250eb0ef9d08a2ad95b5b44dbc2a10580c00a8c1375553`. Its 1,305 active payloads omit Ghoul (physical 149) and Giant Spider (329); Skeleton (273) loses its printed type/Actions headings; Flameskull (135) loses the second column's traits, spellcasting and actions; two different stat records are incorrectly labeled Zombie (317). The Young Green Dragon record (95) includes the adult dragon's actions and legendary budget before the actual young dragon's traits/actions. A single AC/HP pair therefore does not establish complete, clean source boundaries.

`db/vector/creature_sources.py` supplies narrowly reviewed records only for these exact immutable PDF bytes. Physical page rectangles and column order were checked against rendered originals. Missing raster headings/type/Actions labels and the clipped Flameskull phrase `an action.` are explicitly recorded; all statistic text is retained from the source. Each receipt contains PDF checksum, physical page, per-region bounds, raw region text digest, selection offsets, parser/version, page/extracted/record text digests and reviewed labels. Ordinary complete records keep their active-index origin. The source PDF bytes are checked before extraction, and extraction uses those same bytes. Unrelated checksums/editions, conflicting sources and unresolved ambiguities cannot use these recipes. Zombie and Ogre Zombie remain separate exact identities. The only source name alias is the explicitly verified MM `Flameskull` → `Flame Skull`; the helper `canonical_source_creature_name` exposes the same binding to compilation and runtime. No global space removal or similarity lookup is used.

The source-specific actor inventory checked original Phandelver PDF SHA `c3095b66e2f690d8a16b43a6b73c0aa6209c2e913714df5a65b3d2d0481aec07` against rendered physical pages 57, 59 and 61. The exact retained extraction names/spans used for local grounding were:

| Exact extraction stat heading | Physical page | Original character span |
| --- | --- | --- |
| Evilmage | 57 | 1581–2393 |
| Mormesk The Wraith | 59 | 1438–2661 |
| Nezznar The Black Spider | 59 | 2876–4230 |
| Sildar Hallwinter | 61 | 2388–3211 |
| Redbrand Ruffian | 61 | 1783–2248 |

The retained extraction remains immutable. Source-checksum/page/name-bound parsing corrections explicitly record the original token, replacement, original character offsets and reviewed PDF bounds: Evil Mage's `l st Level`/`ld8`; Mormesk's `6dS + lS`/`3dS + 3`; Nezznar's `6dS`/`ray offrost`/`invisihility`; Sildar's `ld8`/`ldl0`. These glyphs were read from the printed source, not inferred from monster averages or substituted statistics. Module evidence retains the original stat text/hash plus the corrected parsing-text digest and each correction. Preparation and runtime resolution use the same helper; combatants retain correction/source proof in their private source reference. Engine parsing now recognizes Markdown section-heading decoration and exact alternative spellcasting grammar/typographic separators without changing source text. The CR/XP parser accepts whitespace around printed thousands separators while still cross-checking the code-owned table.

Read-only production grounding results, using the frozen books and actual Bryn snapshot, are saved locally at `/tmp/edgar-source-readiness/full-grounding-preflight.json` and `/tmp/edgar-source-readiness/module-grounding-preflight.json`. All 20 exact names used by saved encounter rosters or existing actor templates resolve. All 23 saved encounter requests now produce source-backed receipts, with no source/parser or declared required-mechanic findings. All five inspected named/module stat blocks also ground with exact passage proof and executable attacks; Evil Mage exposes listed Magic Missile/Hold Person and Nezznar listed Magic Missile. Young Green Dragon now has its own printed Bite/Claw/Poison Breath, correct young Multiattack, and no phantom adult legendary actions.

These successful source receipts do not certify encounter suitability or full rules support. All 23 saved encounters calculate Deadly for Bryn at level 1; 15 rosters are reduced to two participants, including populations 12→2, 10→2 and 9→2. The final saved encounter inventory retains 207 adaptation/runtime omission notes. Flameskull has executable listed Magic Missile, while Fire Ray, five other listed spells and unsupported traits remain disclosed. Ghoul paralysis, Giant Spider web/poison effects, Mormesk's HP-maximum drain, Nezznar's innate abilities/staff poison, Sildar's Parry/general multiattack wording and other unsupported mechanics are not claimed as implemented. Room population grouping, significant participants, progression and mandatory fight design still require the broader semantic repair. Twig Blight's MM stat heading remains `Twic Blight`; it is outside the retained requested roster and has not been silently certified or aliased.

Verification here consisted of syntax parsing of changed files, original PDF checksum checks/rendered page review, and read-only live source/encounter inventories. No test suite, CI/CD, model request, embedding, vector/index write, campaign checkpoint mutation, commit, restart or campaign continuation was performed by this source-grounding subtask.

## Integrated remediation and preserved recovery

The requested GPT-6.1 Sol extra-high review and follow-up integration review are complete. Local implementation now separates canonical actor names/private descriptions/stat headings, supports source-specific module bindings and distinct named actors sharing a template, preserves explicit actors during solo fitting, represents alternative routes consistently, and compiles automatic observations/handovers independently from check decisions. Prepared quest authorization and latest mutable facts are shared with runtime. Milestone ownership also determines required milestone membership.

The path proof now requires automatic-action, outcome, gated-route and ending witnesses plus one joint mandatory-completion path. Grounded combat facts require exact actor-specific receipts. All effects within a policy validate against the pre-action scene; one action can move to only one destination. A finite optimistic dependency closure first reports impossible missing/circular producers, with results saved beside the retained proposal. Possible membership never substitutes for exact witnesses. Exact exploration prioritizes completion and saves growing search partitions without an overall stopping cap. P10 independently repeats proof after review.

Known response usage remains separate from unresolved estimates; exhausted credits are classified before output parsing. Attempt counts and cost/token/call estimates do not stop preparation. Corrections retain the authoring contract, full diagnostics and advisory source review. Fresh PDF commissions select the compact pipeline and review partitions when the real provider context requires it. These changes do not establish the retained proposal's semantic validity: the unchanged retained blueprint still needs focused repair for its missing causal advancement/ending and encounter composition.

The held run remains `prep-95ea542c81af43ec9dea3f61a77e9f85`; its accepted studies/design and original benchmark start remain unchanged. Verified snapshot `review-fixes-preserved-20261020T135726Z` contains 78 original files; campaign archive SHA256 `e4c9f149ad1b4ea88b1d23fc1f045d8c5149f82c7d5e94b78c8060e96b70e7cd`. All 22 accepted artifact digests and the cancelled comparison baseline are preserved. No Edgar provider calls occurred during review or implementation.

All 26 changed Python sources parse, production imports/schema generation pass, all changed Python files pass Ruff, and `git diff --check` passes. No test suite, CI/CD or remote push was run. A coherent local build/restart, receipt-accounting reconciliation and same-run resumption follow these checks; fresh end-to-end performance is still unmeasured.

## Follow-up review of the corrected blueprint — general contract remains incomplete

The resumed correction completed, but P04 then stopped on missing Sildar `released`
producers. GPT-6.1 Sol extra-high reviewed the newest saved proposal before any
further paid generation. This exposed a general contract gap: canonical prepared
validation provisionally admits combat dispositions from an actor's stat reference,
while runtime accepts them only from exact persisted actor-bound combat outcomes
and P10 requires accepted grounding receipts. Ordinary unbinding/rescue actions
should instead complete narrow source-backed rescue milestones. A broad objective
that permits establishing a captive's death cannot authorize living conversation.

The definite missing roots and dependent transitions are:

| Missing root | Immediate consumers | Further blocked progression |
| --- | --- | --- |
| `yeemik:captive` | T1 | `castle_goblins`, T28 |
| `sildar:released` | T3, T4 | account and objectives 0101/0102/0103 |
| `dendrars:released` | T13, T17 | Mirna directions and objectives 0301/0302/0303 |
| `gundren:released` | T39 | objectives 0501/0503 |
| `nundro:released` | T48 | objective 0703 |

These roots also block T51, T52, T53 and T54's ending. The complete blocked set
is T1,T3,T4,T13,T17,T28,T39,T48,T51,T52,T53,T54. All fifty scenes are possible
under optimistic joining, and no additional positive dependency cycle was found.
This is negative dependency evidence, not a full executable path witness.

General code remediation now collects this finite optimistic dependency inventory
alongside structural preflight instead of stopping on the first missing producer.
Only exact named encounter participation with matching actor/stat requests admits
hypothetical combat events. Statuses/results are joined and resource/disclosure
restrictions ignored, so failure implies no producer chain, while possible
membership never permits acceptance. Movement effects are included optimistically.
Local evaluation of the actual retained proposal returns exactly the twelve
independently identified blocked actions. The authoring contract distinguishes
rescue milestones from combat dispositions and requires causal preconditions in
typed guards instead of action prose. Recognized final authored producer findings
are mapped to blueprint paths; compiler/source failures remain local.

The same review found seven additional source/action concerns, which are separate
from guaranteed dependency impossibilities:

1. T14 assumes a successful DC15 Intimidation check but only guards on Mosk captive.
2. T39 omits Gundren's required map before extraction (source span 2107).
3. T47 assumes the prison door was opened by key or DC15 tools; the D6 door change
   explicitly represents reaching the door without opening it (2591–2593).
4. T5's post-flood discovery has no actual flood-event prerequisite.
5. T1/D0 bargain and conscious-captive conditions remain partly in prose.
6. T11 checks only Mosk even though two other bugbears can still control Droop.
7. D7 custody arrangements lack captive/receiving-authority guards; this is lower
   severity because their effects record arrangements rather than transfer custody.

Minor procedures may stay in DM notes. The issue arises when an executable
producer treats an unperformed prerequisite as already earned. Do not serialize
every room detail to compensate; add only the durable action/state required for
the intended consequence.

Source binding also cannot yet express all template-plus-narrative modifiers:
generic Bugbear/Wolf requests lose Grol's printed 45HP (2083) and Snarl's 18HP
(2087); Yeemik has 12HP (545) if his exact encounter is restored. Those sentences
are not complete headed module stat blocks. This requires a generic local source
modifier representation with source proof, not a prompt asking for fabricated
statistics or a module binding to an incomplete sentence. Earlier checksum-bound
OCR recipes remain specific to the inspected source PDFs; they do not establish
general PDF parsing reliability.

Recovery now retains the newest successful paid proposal through a local failure,
instead of preferring older rejection diagnostics. It keeps exact task, binding,
dependency and logical-call identity, cumulative usage receipts, and revalidates
the retained proposal normally. Cause-chain diagnostic records remain private.
The reviewer found no blocking regression in these follow-up changes.

Paid generation is held. Snapshot `causal-review-hold-20261020T142128Z` verifies
85 campaign/job/source files and all 22 prior accepted artifact digests, including
the latest correction. The run remains blocked in P04; studies/design were not
repeated. New-PDF reliability, complete campaign acceptance, and the twenty-minute/
five-dollar target remain unproven. The latest correction took about 9.6 minutes,
bringing compact recovery estimates to USD 5.105084 before completion.
