# EDG-055 product evaluation — run of 20 October 2026

Owner-instructed run of the [EDG-055 evaluation procedure](../work-register/stories/EDG-055-evaluate-full-campaigns-fun-human-ux-and-accessibility.md#evaluation-procedure--owner-instruction-19-october-2026-binding),
driven by the orchestrator in the built-in browser pane (no subagents). Owner choices: **Lost Mine of Phandelver** PDF;
scope **play-through plus the narrator set and the classification evaluation**; caps USD 10 (narrator) and USD 5
(classification); play-throughs uncapped with a USD 50 per-stage runaway breaker.

**Outcome: the run stopped before any play.** Both campaign preparations failed closed before a single provider call.
The PDF failed the extraction quality gate. The AI campaign failed because the active rulebook index has records
without a heading. Neither campaign could be started, so the gameplay checklist was not exercised. Stage A tooling is
not present, so neither Stage A evaluation could run as specified. **Actual provider spend: USD 0.00.** Nothing in the
product was fixed during the run.

## Environment

| Item | Value |
| --- | --- |
| Tested revision | `main` at `23ceb32` (Merge PR #127), images `edgar-main-api` / `edgar-main-migrate` rebuilt from it |
| Stack | `edgar-main-*` containers (API, Postgres 16, Redis 8.6.3, Qdrant 1.16.3), compose project `edgar-main`, worktree `~/.claude-worktrees/edgar-b/edgar-main-run` with `docker-compose.local-main.yml`, on the copied data volumes |
| Frontend | The UI bundled in the API image (same commit), `http://127.0.0.1:8000/ui/`, auth mode `local`, user `dm@edgar.local` |
| Browser | Claude desktop built-in browser pane, viewport about 793×811, read by DOM/text |
| Models (configured) | Play `gpt-6-luna` (effort low); preparation `gpt-6.1-sol` (high); embeddings `text-embedding-3-small` |
| Limits (configured, not changed) | Daily spend ceiling USD 50 (`LIMIT_DAILY_SPEND_MICROUSD=50000000`), assumed cost per unknown-cost call USD 0.10, unbudgeted job reservation USD 0.50, upload max 64 MiB |
| Rulebook index | PHB 2774 points (2773 at active version `d22ac84b…`), DMG 2236 (2235 at `fa218009…`), MM 1306 (1305 at `9672493b…`); `discover_book_bindings('2014')` returns exact PHB, DMG and MM bindings |

### Environment steps taken before the run (not product fixes)

1. The running stack was on `93cbd5d` (PR #91), 124 commits behind `main`. The worktree was moved to `origin/main`
   (detached) and the images rebuilt.
2. **The stack's database was on an unmerged branch migration.** `alembic_version` was `25_table_pacing_queued_actions`
   (the EDG-051 branch numbering), unknown to `main`. `/ready` already reported `schema: unavailable` before anything was
   touched. The branch file and `main`'s `22_table_pacing_queued_actions` have identical bodies; only the IDs differ.
   The chain was realigned without deleting data: version row set to `20_spotlight_turn_arbitration`, `upgrade` to
   `21_combat_always_confirm` (adds `campaigns.combat_always_confirm`, default false), `stamp` at
   `22_table_pacing_queued_actions`. After this, `/ready` reported all checks ok.
3. The built-in browser cannot drive the native file picker. The PDF was served from a local CORS file server, then
   attached to the UI's own `<input type=file>` with a `DataTransfer`. The UI's upload code did everything else.
4. `uv run` re-synced the local `.venv` of the main checkout to run the offline boundary evaluation.

## Timeline and readiness states

| Time (UTC) | Event | Readiness shown |
| --- | --- | --- |
| 07:29:23 | Hero **Bryn Ashvale** (Fighter L1, HP 12) created through Characters, submitted with Enter | — |
| 07:30:33.39 | Phandelver PDF accepted (55,961,050 B, 2014, hero Bryn, budget USD 50) — `202` | Card: "Processing · Preparing…" |
| 07:31:13 | Backend: job `failed`, stage `preparation`, `failure_code=validation_failed`, support ref `err-0c4a71fbd8093891` (about 40 s after upload). My search found no log line, but it was unreliable (see the F4 correction). | Card unchanged: "Processing", "Preparation is still running" |
| 07:33 (reload) | API `/api/adventures`: `status=processing`, `failure_code=null`, readiness `preparation_in_progress`, `preparation_setup_required=true` | Card still "Processing"; setup form shown with empty hero, **Start disabled**; More → only "Delete campaign" |
| 07:34:10 | Generate with AI, Medium, hero **Maelis Thorne** (Wizard L1), budget USD 50 → `429 daily_budget_exhausted` (retry after 59,150 s) | Form: "The Dungeon Master service is receiving too many requests. Check save status before retrying." |
| 07:35:01.86 | Same request with budget USD 20 → `202` | New card "AI campaign" |
| 07:35:02.15 | Preparation P01 accepted, P02 `study_books` blocked: `stage_evidence_invalid`, public `turn_failed`, ref `err-0a9630447f87b69d` (0.3 s, 0 provider calls) | Card: "Failed · Something went wrong while processing this action… Saved preparation work can be retried. (twice) · Unavailable" |

## Checklist (procedure step 4, both campaigns)

Status key: **Pass**, **Fail**, **Blocked** (not reachable because preparation failed), **Not run** (outside what was
reachable). Both campaigns have the same status for every gameplay row, because neither reached play.

| # | Check | PDF (Phandelver) | AI Medium | Evidence |
| --- | --- | --- | --- | --- |
| 1 | Upload / generate from the UI | Pass (accepted) | Pass on retry (first attempt refused, F7) | `adventure_upload_accepted`, `adventure_generate_accepted` |
| 2 | Preparation and embedding reach Ready | **Fail** | **Fail** | F1, F2 |
| 3 | Readiness state is truthful | **Fail** (false "Processing") | Partial (Failed, but generic cause) | F3, F4, F5 |
| 4 | Opening scene and grounding in module | Blocked | Blocked | — |
| 5 | Exploration and seven-turn recall | Blocked | Blocked | — |
| 6 | Skill checks only when uncertain (EDG-170) | Blocked | Blocked | — |
| 7 | Combat: initiative, attacks, spells, conditions | Blocked | Blocked | — |
| 8 | Reactions and Ready | Blocked | Blocked | — |
| 9 | Grapple, shove, off-hand, action economy | Blocked | Blocked | — |
| 10 | Death saves / subdued mode (EDG-045), foe behaviour | Blocked | Blocked | — |
| 11 | Rests, hit dice, Second Wind | Blocked | Blocked | — |
| 12 | Inventory, gear shortcuts, rope restraint, nonlethal capture | Blocked | Blocked | — |
| 13 | NPCs, promises, clues with journal and map provenance | Blocked | Blocked | — |
| 14 | Failed clues | Blocked | Blocked | — |
| 15 | Save and reload at several points | Blocked | Blocked | Campaign list state survived page reloads (not a save test) |
| 16 | Character sheet (AC, spells, proficiencies) | Blocked | Blocked | Class presets shown on Characters page |
| 17 | Multi-part turn confirmation, declaration contract (EDG-182) | Blocked | Blocked | — |
| 18 | Secret handling on player surfaces | Pass (limited) | Pass (limited) | Failure copy, API projection and cards showed no source text; only failure pages were inspected |
| 19 | Error and recovery states | **Fail** | **Fail** | F3–F7 |
| 20 | Keyboard operation of main flows | Pass (limited) | Pass (limited) | Landing: Tab focuses "Enter the Campaigns" (visible focus class), Enter opens it; Create character submitted with Enter. Play flows not reachable. |

## Comparison (procedure step 6)

| Measure | PDF (Phandelver) | AI Medium | Note |
| --- | --- | --- | --- |
| Preparation time to terminal state | about 40 s (extraction + gate), then shown as running indefinitely | 0.3 s | Both fail closed before any model call |
| Preparation cost (actual) | USD 0.00 (local OCR, no embeddings) | USD 0.00 (0 provider calls) | Daily counter still charged USD 0.50 and USD 20.00 (F6) |
| Readiness outcome | Failed, but displayed as Processing | Failed, generic message | — |
| Grounding and consistency | Not measurable | Not measurable | — |
| Turn latency | Not measurable | Not measurable | No turn was played |
| Clue and NPC quality | Not measurable | Not measurable | — |
| Journal and map completeness | Not measurable | Not measurable | — |
| Failures | F2, F3, F4 (+ F1 would block it next) | F1, F4, F5, F7 | — |

**Verdict:** nothing on today's `main` can create a playable new campaign on this stack. That holds for the owner's
flagship PDF and for an AI-generated campaign. The AI path is blocked by five bad rulebook records. That is a small,
well-located data defect, and fixing it should unblock generation. The PDF path has its own extraction-quality
blocker on four Phandelver pages. Behind both sits a status problem: the PDF failure is shown as endless progress,
which is worse than the failure itself. Fun, grounding and mechanics remain unmeasured for this revision.

## Findings

Severity: Blocker (no workaround in the product), High, Medium, Low.

| ID | Severity | Finding | Reproduction / evidence | Suggested owner |
| --- | --- | --- | --- | --- |
| F1 | **Blocker** | The active rulebook index contains records with no heading, so every preparation fails at P02 `study_books`. `read_verified_book_snapshot` raises `GroundingUnavailable(book_citation_incomplete)` ("The PHB contains a record without an exact page or heading"). These records also carry OCR letter substitution that passed review ("cao be destroyed by dealiog", "Coostitutioo saviog throw"). | Active-version scan: PHB **4** records on p. 270 with `section=None`; DMG **1** record on p. 199 ("RoPE oF ENTANGLEMENT…") with `section=None`; MM 0. Generate any campaign → P02 blocked, 0 calls. | EDG-167 (re-index, Done) — reopen or new story |
| F2 | **Blocker** (this PDF) | Lost Mine of Phandelver fails the extraction quality gate (fail-closed, correctly). The official module cannot be prepared. | `extraction.json`: `quality_passed=false`; p. 5 and p. 43 `partial_native_numbers_lost_in_ocr`, p. 59 `incomplete_stat_fields`, p. 64 `ambiguous_column_gutter`, `chunk_page_coverage` (pp. 5, 13 not covered); 108 warnings, 64 pages, 512 chunks. `load_reviewed_extraction` raises `ValueError: Extraction quality checks failed`. | Extraction lineage (EDG-161–165, EDG-177–180) |
| F3 | **High** | A failed PDF preparation is shown as endless progress. The API projects a private-preparation failure as `status=processing`, readiness `preparation_in_progress`, `failure_code=null`. The card says "Preparation is still running", also asks to "Complete your setup", and its Start button is disabled. "More" offers only Delete. There is no cause, no page list and no preview link, although the extraction preview exists. This is the "false-ready flow" class UX-V03 calls a blocker. | Upload Phandelver, wait 40 s, reload `/ui/campaigns`; compare `data/adventures/<slug>.json` (`failed`) with `GET /api/adventures` (`processing`). | Preparation lifecycle / campaign readiness owner |
| F4 | Medium | Failure codes are mislabelled and not traceable. (a) The PDF failure is stored as `validation_failed`, a generic code for an extraction-quality refusal. (Correction: I first reported that no log line existed. That search was unreliable, because the container log contains colour bytes that made `grep` treat it as binary. The container has since been recreated, so the claim can't be re-checked. On the rerun, `preparation_stage_failed` was logged with its support ref.) (b) `book_citation_incomplete` is not on P02's allow-list, so it is collapsed to `stage_evidence_invalid`, then shown as play-turn copy ("Something went wrong while processing this action. Check the saved result…"), with recovery `contact_support` and no hint that the rulebook index is at fault. | `preparation_stages.py` P02 code allow-list; `preparation_runtime._setup_failure_code`; API log contains only `adventure_upload_accepted` | Safe-errors / preparation owner |
| F5 | Medium | The AI card's retry state contradicts itself. It says "Saved preparation work can be retried." twice, yet the action reads "Unavailable". The API has `job.retryable=true` but `failure.retryable=false`. | `GET /api/adventures`, slug `a_frontier_mining_town_…` | Campaign card / readiness owner |
| F6 | Medium | Daily-spend reservations are not released when preparation fails with no spend. The Redis counter for 20 Oct reads USD 20.50 with zero provider calls: USD 0.50 (PDF, unbudgeted reservation) + USD 20.00 (AI budget). Repeated failed attempts can lock all play for the rest of the UTC day. | `GET limits:spend:20261020` = 20,500,000 µUSD after both failures; `preparation.json` `cost_microusd_spent=0` | EDG-066 limits owner |
| F7 | Medium | The daily-budget refusal blames the wrong cause. `429 daily_budget_exhausted` is shown as "The Dungeon Master service is receiving too many requests. Check save status before retrying." It gives no reset time (about 16 h) and does not say the requested budget exceeds today's remaining allowance (USD 49.50 left, USD 50 asked). | Generate with budget 50 after any reservation | Limits / setup form copy |
| F8 | Low (ops) | The shared local stack drifted onto an unmerged branch migration (EDG-051 `25_…`), leaving `/ready` at `schema: unavailable` until realigned. | See Environment step 2 | Ops / stack hygiene |
| F9 | Low | A blank AI title produces the generic card title "AI campaign". It is not derived from the theme (the slug is), so two cards named "AI campaign" are indistinguishable in the list. | Campaign list after generation | Campaign list owner |
| F10 | Info → owner decision | With the configured ceiling, the "uncapped" play-through is capped at USD 50 per UTC day. `gpt-6-luna` has no configured price, so every play call is counted at the assumed USD 0.10. | `limits_configured` log line; `provider_trace.CONFIGURED_PRICES` has only `gpt-6.1-sol` | EDG-184 |

## Stage A

### Narrator evaluation set (former EDG-154, cap USD 10) — not run, USD 0

The collection script was never committed. `tests/fixtures/real_model/README.md` says "The collection script is not
in the repository", and none exists in `tools/` or `evals/`. Running the set as specified first needs that collector
and the two-labeller blind-labelling harness built. That is tooling work, which the ticket allows when it makes no
model calls, but it is not part of an evaluation run. Real narrator collection also needs a playable session, which
F1 currently prevents.

The offline part ran at no cost: `uv run python tools/eval_boundary.py` over the frozen 15 October fixtures.

| Measure | In-sample | Held-out |
| --- | --- | --- |
| Unsupported-action classifier | 120/120 exact (en 99, es 10, it 11) | 30/30 exact |
| Lethality, production (field then words) | 119 correct, 1 FP (U27 "I tackle the goblin to the ground." → nonlethal) | 29 correct, 1 FP (H11 "I knock the goblin captain down with my shield.") |
| Lethality, parser field only | 118 correct, 2 FP (U27; L04 "I stab the goblin captain through the heart." → lethal, expected none) | as production |
| Roster check (real narrations) | 30 samples, **tp=0, fn=0** | 9 samples, **tp=0, fn=0** |
| Narration sanitizer | 2 scenarios, nothing removed | 2 scenarios, 1 claim removed (TP), 0 FP, 0 FN |
| Adjudication sanitizer | 3 engine roll lines, 0 lost | 2, 0 lost |

The roster check has **no positive sample at all**, so its recall is unmeasured. That is the gap EDG-154 was meant to
close by provoking revived and unknown-foe narration.

### Assisted classification of uploaded PDFs (former EDG-181, cap USD 5) — not run, USD 0

The feature is not implemented on `main`. `apps/api/services/solo_review.py` states "Uploads (pdf/text sources) are
classified only by a human", and there is no model-assisted player/planner/secret classifier or labelled sample to
evaluate. Building it is delivery work, not an evaluation run.

## Spend ledger

| Stage | Provider calls | Tokens | USD (actual) | Daily-limit counter (reservation, not money) | Notes |
| --- | ---: | ---: | ---: | ---: | --- |
| B — PDF upload and preparation | 0 | 0 | 0.00 | 0.50 | Failed at extraction gate; local OCR only; no embeddings |
| B — AI Medium generation (attempt 1) | 0 | 0 | 0.00 | 0.00 | Refused 429 before start |
| B — AI Medium generation (attempt 2) | 0 | 0 | 0.00 | 20.00 | P01 local, P02 blocked before its call |
| B — play-throughs | 0 | 0 | 0.00 | 0.00 | Not reached |
| A — narrator set | 0 | 0 | 0.00 | 0.00 | Collector absent; offline re-measure only |
| A — classification evaluation | 0 | 0 | 0.00 | 0.00 | Feature absent |
| **Total** | **0** | **0** | **0.00** | **20.50** | Runaway breaker (USD 50/stage) never approached |

**Real money spent in this run is about USD 0.00–0.01.** Edgar recorded no provider call. The owner reports about
1 cent on the provider side; Edgar's ledger does not attribute that. The USD 20.50 column is only Edgar's internal
daily-limit counter: budget reservations that were never released after the failed jobs (F6). It is not a charge.

Sources: `preparation.json` usage attempts (`provider_called=false`, `cost_microusd_spent=0`), the PDF job metadata (no
preparation run created), Redis `limits:spend:20261020`.

## Data left on the stack

No data was deleted. New records: hero **Bryn Ashvale** (id 10); adventure `lost_mine_of_phandelver_edg_055_eval`
(failed, shown as Processing); adventure `a_frontier_mining_town_troubled_by_bandits_goblins_and_a_lost_dwarven_mine`
("AI campaign", failed). Hero **Maelis Thorne** (id 8) was used for the AI setup. The `edgar-main-run` worktree is now
detached at `23ceb32`, and the database chain is at `22_table_pacing_queued_actions` (head).

## What a rerun needs

1. Repair or exclude the five heading-less rulebook records (F1), then confirm `read_verified_book_snapshot` succeeds
   for PHB, DMG and MM.
2. Choose the PDF: Phandelver stays blocked until F2's pages pass, or the owner picks another file.
3. Optionally fix F3 and F6 first, so a failed attempt neither looks like progress nor consumes the daily ceiling.
4. For Stage A: commission the narrator collector and labelling harness, and the PDF classification feature, as
   delivery work before their capped evaluations.

## Follow-up after the run — tolerant PDF pipeline (owner instruction, 20 October 2026)

The owner asked for a pipeline that can read the PDF despite OCR failures, which can never be driven to zero:
inspect why pages fail, then handle the failures generically, with no page-specific fixes.
[PR #129](https://github.com/smaniot96/Edgar/pull/129), stacked on #128, does this.

**Why each Phandelver page failed** (inspected against the page images):

| Page | Content | Cause | Handling now |
| --- | --- | --- | --- |
| 5, 43 | Full-page maps | The only native text is the folio; the OCR route drops footers, so the "numbers lost" check fired on the page number. The same page passed in one OCR environment and failed in another. | Folios and running heads (outer 6% bands) are not compared. Pages pass. |
| 13 | Full-page art | Nothing chunkable | Picture-only, not required for coverage (#128) |
| 59 | Three stat blocks | The scanner's embedded text layer reads 8 as S ("Armor Class lS", "2dS + 2"); 6 dice expressions and 1 AC in the whole book are affected | Kept as a degraded page with its cause; the study is told to treat its numbers as unverified and prefer the rulebooks |
| 64 | Rules index and credits | Ambiguous three-column gutter | Kept as a degraded page |

**Policy:** uploaded PDF adventures may carry degraded pages up to 10% of content pages (minimum 1). Each one is recorded
with its causes in the readiness report and logged. Beyond that, the upload is rejected with the failed-page count.
Whole-document problems are never absorbed, and rulebooks stay strict. For F1, rulebook records without text, page or
heading are left out of verified evidence (logged) up to 1% of a book; beyond that the book is unavailable.

**Verified offline (no model calls):**
- Phandelver now passes readiness: degraded pages 59 and 64 (allowance 6), pages 5 and 13 picture-only, 512 chunks.
  The strict policy still rejects the same bundle.
- Against the stack's real index, the rulebook snapshot opens: PHB 2769 of 2773 records, DMG 2234 of 2235, MM 1305.

**Not yet verified:** live preparation and play of Phandelver. That needs the branch deployed to the stack and real
preparation calls, which is the owner's decision. F3 (failed preparation shown as "Processing"), F5, F6 and F7 are
unchanged by #129.

## Rerun on PR #129 (`c5ae4fd`, CI green) — 20 October 2026

| Time (UTC) | Event |
| --- | --- |
| 08:03:15 | `edgar-main` stack restarted on `c5ae4fd`; `/ready` all ok |
| 08:04:01 | Phandelver uploaded through the UI (hero Bryn Ashvale, budget USD 15, 2014) — `202` |
| 08:04:43 | Extraction passed the gate (degraded pages within the allowance); preparation running, stage "Checking sources" (P02) |
| ~08:05 | `P02:study_books` **accepted**: rulebook snapshot opened live (F1 resolved), 1 Sol call, 27,527 in / 988 out tokens, USD 0.130 |
| 08:06:13 | `P02:study_source_0` (first of 18 source groups) failed `provider_unavailable`, ref `err-60ef2072b2e9b928`; run **blocked** |

**New finding F11 (Blocker for PDF preparation): preparation calls time out at 60 s.** The `preparation.p01`–`p12`
profiles in `config/model-profiles.toml` set no `timeout_seconds`, so they inherit the 60 s default. The `authoring.*`
profiles doing the same Sol/high work set 180–240 s. A Sol/high study of an 18,000-character source group outlasted the
limit (the previous Sol call returned at 08:05:09 and the failure was logged at 08:06:12, about 62 s later, with no HTTP response for the call; `APITimeoutError` maps to `provider_unavailable`; `preparation_stage_failed` was logged with ref `err-60ef2072b2e9b928`).
Because the outcome is unknown (the provider may have billed), the supervisor correctly refuses an automatic retry and
blocks the run, charging the call's cost ceiling (USD 0.632). Every PDF large enough to need a long study call will
block the same way. The card shows "Failed · repair preparation" with retry offered. A retry under the same timeout
would most likely fail again.

Spend for this stage so far: 2 provider calls, 28,515 known tokens, USD 0.130 known plus up to USD 0.632 for the
unknown-outcome call (USD 0.762 charged by the run). Daily-limit counter: USD 21.00.

### Second rerun on `2abff18` (300 s preparation timeouts, CI green), daily ceiling raised at the owner's request

The owner waived price constraints for this run, because Edgar's cost estimates are wrong (timed-out calls are
charged at their USD 0.632 ceiling; failed jobs keep their reservations). The stack's `.env` sets
`LIMIT_DAILY_SPEND_MICROUSD=1000000000000`, and the preparation budget is USD 1,000.

**F12 (Medium): a failed preparation cannot pick up corrected configuration.** The blocked first rerun offers only
"Saved preparation work can be retried." (a button, beside a disabled "Unavailable") or Delete. A retry resumes the
frozen run, and the provider builds its client from `binding.profile_snapshot`, so it would keep the 60 s timeout
and the USD 15 budget. There is no "restart with current settings". The PDF was uploaded again as a new campaign
("…rerun 2") instead; nothing was deleted.

| Time (UTC) | Event |
| --- | --- |
| 08:25:51 | Phandelver uploaded again (hero Bryn, budget USD 1,000) |
| 08:26:39 | Extraction gate passed; P02 running |
| 08:27:09 → 08:31:20 | `study_books` and `study_source_0`–`2` accepted, about 70–110 s per group (over the old 60 s limit, so the timeout fix works) |
| 08:33:48 | `study_source_3`: HTTP 200 after **154 s**, then `ValidationError` on the structured output → `turn_failed`, `usage_known=False` → not retryable → run **blocked** (ref `err-11bbe874f0936891`) |

**F13 (Blocker): one invalid model answer blocks the whole preparation.** A completed call (HTTP 200) whose structured
output fails validation is recorded as an unknown provider outcome. Its usage is not read from the raw response, so
the supervisor refuses to retry and charges the call's ceiling (USD 0.632, 134k tokens). The likely cause of the
invalid answer is truncation: `preparation.p02` allows `max_output_tokens = 7000`, which must also cover Sol's
high-effort reasoning tokens, and a dense group ran 154 s. Model output will sometimes be malformed, just as OCR
sometimes fails, so the pipeline must absorb it: record the known usage, retry within the per-task attempt limit,
and give reasoning room in the output cap.

Spend for this stage: 5 provider calls (4 accepted), 51,652 known tokens, USD 0.397 by Edgar's configured rates,
plus a USD 0.632 ceiling charge for the invalid answer (run total USD 1.029). Real provider cost is not observable
from Edgar.

**F14 (Medium, UX): a new preparation is hard to find.** After the third upload (08:58:47) the owner, watching the pane,
could not see the campaign in progress. Its card ("Processing · Checking sources · Cancel preparation · Preparing…")
was 11th of 15 in the list, below older and failed campaigns, because a never-played campaign sorts low. The page shows
no progress indicator near the top, and the dialog closes without scrolling to or highlighting the new card. A new or
running preparation should appear first, or be pinned in a progress strip at the top.

**F15 (Low, UX/accessibility): deleting a campaign depends on the browser's native `confirm()`.** In the built-in
browser pane, native dialogs are suppressed and `confirm()` returns false, so the owner's clicks on More → Delete
campaign did nothing: no request was sent and no message was shown (console: "Page dialog suppressed (confirm): Delete
campaign "Velmoor (PDF)"? This cannot be undone."). An in-app confirmation dialog would work everywhere, give feedback
and be accessible.

**F16 (Medium): the campaign card's "Delete campaign" deletes the adventure source, not the campaign.** On the campaign
list (`Home.tsx`), the card's More → Delete campaign calls `DELETE /api/adventures/{slug}`. Deleting the Playtest cards
removed their sources and Qdrant collections, but the campaigns and their saves stayed, now shown as "Source
unavailable". A second click, or a click on a campaign without a source (Chalice homebrew, the old Phandelver), returns
404 and the banner "That saved item could not be found" (e.g. `err-94d0c679dccc0ec8`). Those cards can't be removed from
the list. The real campaign delete (`DELETE /api/campaigns/{id}`, "Delete campaign and all its saved playthroughs")
exists only on the campaign's own page. The card action should either delete the campaign or say "Delete adventure
source" and explain what remains.

Also observed: the first upload of the day ("EDG-055 eval", failed at 07:31) still shows a second "Processing" card
next to the real rerun 3 (F3; fixed by PR #130, not deployed on this stack).

### Third rerun on `147bd3b` — source study complete, design refused before dispatch

| Time (UTC) | Event |
| --- | --- |
| 08:58:47 | Phandelver uploaded (rerun 3, hero Bryn, budget USD 1,000) |
| 08:59:30 → 09:30:15 | P02 complete: rulebook study plus all 18 source groups accepted, no failures (1.5–2.5 min per group; `study_source_3`, which broke before, passed) |
| 09:32:15 | P03 `design_campaign` refused **before dispatch** with `task_context_limit` (ref `err-4e42eaf783547012`); run blocked, card shows `validation_failed` |

**F17 (Blocker for full-size modules): the design input is measured in bytes, and the guidance is sent with heavy metadata.**
The provider's predispatch check treats every byte of input as a token. Phandelver's design input (18 source studies
of 280 KB plus 183 KB of rulebook guidance) counts as about 470k "tokens" against the 128k cap. Measured with the
`o200k_base` tokenizer, it is 122,978 tokens: studies 61,600 and guidance 61,378. Of the guidance, 28,447 tokens are
page, section, policy and rule text; the rest is the same book binding and hashes repeated on all 152 excerpts.
Design with lean guidance would be about 90k tokens. A module several times larger (Tomb of Annihilation) would
exceed the cap even with real counting, and needs the source studies condensed in stages before design.

Spend for this stage so far: 19 provider calls, 193,307 tokens (all known), USD 2.115 by Edgar's configured rates.


### Recovery continuation — advisory budgets deployed, 20 October 2026

The owner instructed preparation to continue regardless of token, call and cost caps.
Commit `e3a2128` on [PR #129](https://github.com/smaniot96/Edgar/pull/129) makes those
estimates advisory at dispatch, provider completion, artifact acceptance and checkpoint
loading. Usage and reservations remain recorded; overruns log warnings. The runtime no
longer ends after an iteration count derived from the call estimate. Failed-answer attempt
limits, repair-round limits, cancellation, leases, source integrity, model policy and final
readiness validation remain enforced. The provider enforces its actual context window and
request output limit. This change covers the semantic preparation pipeline; it does not
remove global daily admission limits or the separate optional image-generation budget.
The existing stack daily allowance remains at the owner-raised value described above.

Provider input estimates now use `o200k_base` tokenization instead of UTF-8 bytes. Guidance
sent to preparation omits repeated integrity metadata while retaining the edition, requested
references, book role/source, physical pages, heading, policy and full rule text. The exact
saved receipt remains unchanged and continues to be validated independently.

Measured against rerun 3's actual saved dossier:

| Measure | Before | After |
| --- | ---: | ---: |
| Rulebook guidance tokens | 61,378 | 32,025 |
| Rulebook excerpts | 152 | 152 |
| Excerpt text preserved exactly | — | 152/152 |
| Source study tokens (JSON) | 61,539 | 61,539 |

Validation: 67 targeted provider/supervisor/durability/pipeline/runtime tests passed on the
fix branch; another 35 book/grounding/review tests passed on the combined build. Ruff and
`git diff --check` passed. Regression coverage verifies that valid provider results exceeding
task estimates are accepted, and that a run exceeding its frozen totals can dispatch, save
and accept artifacts. CI is tracked separately below.

The combined local build is `c1a019e` (merging `e3a2128` into the existing #129/#130/#131
combination). Both API and migration images rebuilt successfully. The API was recreated at
approximately **11:58 Europe/Rome**; `/ready` returned all checks `ok`. All **20 accepted
artifact content hashes** match the pre-restart snapshot. Rerun 3 remains **blocked at P03**,
with its **19 provider calls, 193,307 known tokens and USD 2.114524 recorded cost** unchanged.
No model call was made during this recovery and no preparation work was deleted.

**UI continuation pending:** this session's built-in browser runtime reports no available
browser. Opening `http://127.0.0.1:8000/ui/campaigns` through the app returned `queued`.
The owner has been asked to bring this chat and browser pane into view. No API retry was
substituted for the required UI action. Ready, restart-after-Ready persistence, play with
Bryn and the gameplay checklist are **not yet verified**.

Recovery spend ledger: **0 provider calls, 0 tokens, USD 0.00**. Rerun 3's prior spend remains
at 19 calls / 193,307 tokens / USD 2.114524 by Edgar's configured accounting rates; no claim
is made about the provider's invoice.


### Rerun 3 resumed — 12:00:58 Europe/Rome, 20 October 2026

After the owner's explicit instruction to resume rerun 3, the normal application retry
endpoint returned HTTP 202 at 10:00:57 UTC. The built-in browser connection still returned
no available browsers, so this was an API retry; it does not verify the UI retry action.
The same run (`5d81406d90104c45b253c652a44ceee0`) resumed P03 `design_campaign`.
All 19 P02 tasks remain accepted, and all 20 pre-existing artifact hashes are unchanged.
No extraction or study was repeated. At dispatch, the recorded prior usage remained
19 calls / 193,307 tokens / USD 2.114524; new usage will be added when the call settles.
The owner also confirmed that deletion is working on the restarted combined stack.


### Rerun 3 after recovery — design returned, semantic evidence rejected

**Current outcome: blocked at P03; Ready and play have not been reached.**
At 10:02:59 UTC (12:02:59 Europe/Rome), the resumed design request returned HTTP 200 after
about 121 seconds. Actual usage was **94,438 input + 5,595 output = 100,033 tokens**,
USD **0.489652** by Edgar's configured accounting rates. This confirms the byte-based
predispatch block is removed and the lean guidance fits this call.

At 10:03:00 UTC, the executor rejected the result with `stage_evidence_invalid`, support
reference `err-4f2b98025965900b`. It was recorded as known usage and **not retryable**,
so the supervisor blocked the run instead of continuing. The executor replaces the underlying
exception with a generic code and the rejected design was not retained; the exact validation
reason cannot be established from this attempt's saved evidence. `validate_design` checks
unique act/scene IDs, the opening identity and exact physical-page ownership. Those are
possible checks to investigate, not proven causes of this specific failure.

**F18 (Blocker, diagnosis/recovery): a completed design rejected by semantic validation is
neither retained for diagnosis nor retried.** The generic failure loses the reason needed to
repair the prompt or proposal. A follow-up should preserve private rejected proposals and
structured validation diagnostics, then allow bounded correction with that feedback while
retaining the acceptance gate. No such code change was made during this evaluation.

Read-only checks after the failure: `PreparationStore.load_run` verifies the checkpoint;
`load_source` verifies the exact frozen PDF and extraction; 64 nonblank physical pages are
available; all 20 previously accepted artifacts remain saved. Source study was not repeated.
The owner reported deletion working on the rebuilt combined stack.

| Rerun 3 usage | Calls | Tokens | USD, configured accounting |
| --- | ---: | ---: | ---: |
| Saved studies before recovery | 19 | 193,307 | 2.114524 |
| Resumed P03 design, rejected | 1 | 100,033 | 0.489652 |
| **Rerun 3 total** | **20** | **293,340** | **2.604176** |

These are application accounting figures, not a statement about the provider invoice.
No further retry was dispatched after this rejection. No reliable Ready ETA is available
while P03 remains blocked. Gameplay and the required restart-after-Ready test remain pending.


## Rerun 3 recovery — 12:18 Rome, 20 October 2026

The owner requires continuation of the existing run to Ready, preserving extraction and accepted studies.
The run remains `prep-5d81406d90104c45b253c652a44ceee0`. All 20 originally accepted artifact hashes
(P01 binding and 19 P02 studies) still match the saved inventory. No re-upload or study rerun was performed.

The resumed P03 call at 10:09:27 UTC hit the configured 300-second foreground HTTP timeout at
10:14:27 UTC (`provider_unavailable`, `err-4081fceac19012af`). No answer or actual usage was returned.
Accounting conservatively added the reservation (one call, 224,000 tokens, USD 2.432); these are
unknown-usage estimates, not verified provider consumption. Before the next background response,
the checkpoint reports 21 calls, 517,340 accounted tokens and USD 5.036176.

Local fix `c37e3ae`, combined stack `873a3b1`, changes preparation to background Responses jobs.
Edgar persists the provider response ID and private raw response beside the checkpoint, polls without
an overall generation deadline, and retrieves the same request after a worker interruption. Temporary
poll connection/server failures keep the same response ID. Individual HTTP calls still detect broken
connections. The exact frozen model policy remains Sol/high. Rejected schema-valid and schema-invalid
proposals are retained privately with diagnostics for bounded correction attempts. An explicit Retry
grants another attempt batch without resetting attempt counts, usage, the binding or accepted artifacts.

At the owner's request, deployment/resumption did not wait for CI and no further tests were run after
that instruction. The focused local checks already in progress had passed (76). The change is locally
committed; it has not been pushed, so no new CI run was triggered. API `/ready` passed after restart.
The normal retry endpoint returned 202, and P03 attempt 4 was accepted by the provider in background
mode at 10:18:05 UTC. Current state: generating the campaign design; **not Ready yet**.

## Rerun 3 — accepted design and P04 provider recovery

The background P03 response completed at approximately 10:29:39 UTC. Its accepted
design contains **8 acts and 50 scenes**. Known usage: 94,665 input and 20,576
output tokens (115,241 total), USD 0.790180 using configured accounting rates.
The original 20 accepted P01/P02 artifact hashes remain unchanged.

P04's first request was rejected before inference with HTTP 400: the strict
provider schema could not express `CanonicalAct`'s ID-keyed maps. Local commit
`1370146` uses JSON output mode for such dynamic schemas and retains strict
Pydantic/semantic validation in code. Provider rejection details are retained
privately. New HTTP 400 requests record zero inference usage rather than a full
unknown-use reservation; the old failure's reservation remains historical.
Combined stack `90d41de` was built and restarted without tests or CI. The retry
endpoint returned 202. P04 background response
`resp_01c39c17df619708006ad7435815dc87d2a25f91e05d8f3801` is in progress.

The owner requested a subagent audit of arbitrary caps/timeouts and related
recovery defects. Its findings and the inspected local follow-up patch are in
`docs/preparation-reliability-audit-2026-10-20.md`. That patch is not deployed over
the running P04 job. Campaign Ready, post-Ready restart persistence and gameplay
remain unverified.


## Lighter preparation deployed — 13:06 Rome, 20 October 2026

The owner requested substantially fewer semantic calls and authorized sensible small DM additions
consistent with the campaign theme, accepted facts and future acts. Local authoring commit `6eb27b0`
is running on combined stack `45cf7ae`. It has not been pushed or merged into main. No test suite or CI
was run; Python syntax compilation, built-image module imports and API readiness succeeded.

The normal path now authors canonical records, scene packets, encounter requests, resource/progression
notes, private DM notes and asset requirements together once per act. P05 grounds that saved plan
with exact tools; P06 derives the saved scene packets; P07 assembles authored assets plus exact clue
handouts and topology maps; P08 retains original PDF pages with the authored notes and explicit DM
extensions. These four stages no longer need independent model calls on the new PDF path. Legacy
accepted acts can receive one supplementary enrichment call without changing their accepted records.
P09 groups independent reviews using measured request size, compacts repeated evidence, retains
original citation pointers, and verifies exact contiguous text coverage if a long chapter needs slices.
P10 continues local search with larger persisted bounds instead of asking a model to regenerate content
merely because a local search allowance was reached. Later act tasks pin earlier accepted canonical
definitions. Source quote offsets and all residual private source coverage are compiled in code.

For new PDFs that fit, source inventory is local and design reads the exact pages once. Larger inputs
use measured larger study batches. The existing rerun's saved studies and accepted design are reused.
The remaining baseline is eight combined act authoring calls plus independent review batches, with
additional corrections only when actual validation requires them. This is a call-path expectation,
not a provider latency or Ready guarantee.

The old running first-act correction stopped itself with provider `incomplete: max_output_tokens`: all
64,000 output tokens were reasoning, and no usable final answer was returned. No active generation
was interrupted for deployment. The new build includes the earlier advisory-spend/durable-response
fixes through `f0a3999` and does not send the optional request-level output-token allowance. Provider
physical context/output capacity still applies. The normal retry endpoint resumed the same run
`prep-5d81406d90104c45b253c652a44ceee0` at approximately 11:05:52 UTC. Background response
`resp_0bbb768078417b34006ad74b1084a887d2a493c6b76d7f0dd5` is in progress for the first combined act.
All 20 original P01/P02 artifact hashes and accepted design digest
`bbcfb568a5ed6427956d859ad0b7703cf32cec5913c68981160e4c588dc7b374` are unchanged.

Campaign status remains preparing at P04, with no accepted compiled act yet. Ready publication,
post-Ready persistence and Bryn's playthrough remain pending. The older P11 broad-repair rewind and
repair-round stop are still known audit findings; this lighter-path change does not claim to fix them.


## Retained-proposal recovery — 14:02 Rome, 20 October 2026

The three combined first-act responses completed, using 47,265, 51,495 and 51,285 output
tokens respectively. All were retained. Schema diagnostics decreased from 21 to 4 to 2,
but the task blocked at 13:41 Rome with `provider_output_invalid`; no act was accepted.
The final two schema errors were cost-failure trap policies with only encounter effects.
Read-only inspection also found their targets were hazards, while encounter effects require
actors. Changing the failure label alone would not repair that semantic mismatch.

Local commits `db85cf0` and `50acbee`, deployed on combined stack `07eb4b0`, introduce
JSON-pointer repairs of the retained authored proposal, protected existing identities and
unreported source/check fields, distinct durable patch requests, exact dependency checks,
private derived repair receipts and response-ID accounting deduplication. Reusing the
original response does not bill it again in run accounting. Original rejected work remains
unchanged. Independent canonical diagnostics are grouped before a repair request, and
canonical reference checks now run before accepting each act. The full campaign graph,
independent review, index publication and Ready sealing are still required.

Inspection also found `identified_creature` computed the exact stat-text digest for identity
but omitted it from its returned record. The fix returns that digest so source verification
can work. Saved first-act quotes and asset references passed read-only checks. Grounding
now reaches creature mechanics verification and exposes `unsupported_creature_mechanics`
for the saved Goblin/Wolf/Bugbear encounters; that finding is being investigated and has
not been bypassed.

No tests, CI or push were run. Syntax compilation, built-image imports and live API
readiness succeeded. Deployment occurred while the run was blocked. The normal retry
endpoint returned 202 around 14:01:53 Rome. The same run is preparing its first act via
small retained edits in background response `resp_0f57a87682b30e7a006ad75830e43887d2aea15d2009a19623`.
All 20 original source/rulebook artifact hashes and the accepted design digest remain
unchanged. **0/8 acts accepted; campaign not Ready.**

## Saved-edit recovery — 14:14 Rome, 20 October 2026

The next stop was a local OpenAI SDK reload error: persisted JSON-schema format used
`schema_` rather than the API alias `schema`. Commit `18788cf` saves the alias and
reads legacy records compatibly. The same commit allows scalar `/support/dm_notes`
edits, which the first patch guard incorrectly rejected, and recovers completed edit
responses from the exact bound task before making another request. Both previously
completed edit responses and the original authored proposal remain on disk.

Combined build `0386102` is now deployed. At 14:13:58 Rome the normal retry endpoint
resumed the same run. A completed saved correction was applied locally; its remaining
schema defect is now being corrected in background response
`resp_0593bbbd2512d0e6006ad75b0a407887d2b1eb60fe3b867f14`. Batched feedback covers
effect target types/statuses, exact physical public evidence, and entity/source links.
The original 20 artifact digests and accepted design digest were verified unchanged
immediately before retry. **0/8 acts accepted; preparing P04; not Ready.**

Creature grounding now retains parser limitations as explicit runtime adaptation
notes rather than rejecting every stat block for its declared unsimulated movement.
Exact source stats and checksums remain required. Narrow OCR fixes recognize Bugbear
`ll` as 11 only when the printed dice independently match, and preserve the separate
lowercase javelin action. Read-only inspection of the saved proposal passes all seven
encounter groundings with 44 adaptation notes. Goblin Nimble Escape, Wolf Pack Tactics/
Keen senses/prone rider and Bugbear Surprise Attack are still declared runtime
limitations; this does not claim those mechanics are implemented.

No test suite, CI or push was run. Built-image imports and API readiness passed.
All new fixes remain local; `main` has not been updated. Publication, restart
durability, Bryn's playthrough and the known P11 broad-repair issue remain pending.


## Compact recovery held for general contract repairs — 20 October 2026

The original per-act run remains cancelled and preserved for comparison. Compact
run `prep-95ea542c81af43ec9dea3f61a77e9f85` reused all saved studies/design and
retains 22 accepted artifacts. It has not accepted a compiled campaign and is
blocked at P04. The original compact start remains 14:44:31 Rome.

Reviewed source commit `2143b5a` was deployed in combined build `8a34b10`. A focused
correction completed at 16:12:31 Rome, then final validation found missing Sildar
release producers. The latest proposal and raw response are saved. Follow-up
GPT-6.1 Sol extra-high review identified five missing state roots blocking twelve
transitions through the ending, additional unperformed action prerequisites, and
a generic template-plus-source-modifier representation gap. These are structural
preparation/runtime-contract issues; new-PDF reliability is not established.

Local follow-up repairs add optimistic dependency diagnostics before paid repair,
clarify story-rescue milestones versus actual combat dispositions, classify only
recognized authored causal findings as correctable, preserve the newest paid
proposal across local failures, and retain private cause-chain diagnostics.
Read-only validation of the saved proposal returns the same twelve blocked
transitions as the independent review. Syntax/import/schema/Ruff/diff checks
passed; no test suites, CI/CD or remote pushes ran. The newest follow-up edits
have not restarted the live stack. Port 8000 remains healthy.

Paid preparation is held pending the remaining general contract/source repairs.
Known compact usage is eight submitted requests (seven completed, one exhausted-
credit rejection), 856,547 tokens, estimated USD 5.105084 before acceptance.
Imported studies/design and old baseline spend are excluded; this is an estimate,
not an invoice. The twenty-minute/USD5 fresh-PDF target is not yet demonstrated.

Verified snapshot `causal-review-hold-20261020T142128Z` preserves all 85 campaign/
source/job files, including the latest correction; all 22 accepted artifact digests
match the prior backup. Archive SHA256:
`5e97f8f4c92a2b0baf55087fedacf738c86946e1326ac0fd3da4d59cd7efd22e`.
The source/bindings, accepted design, paid proposals, receipts and cancelled
baseline remain retained. EDG-055 stays Open. Ready, restart durability, Bryn's
playthrough and general fresh-PDF acceptance remain unproven.
