# Campaign replay readiness — 23 October 2026

The five requested implementation priorities are addressed and validated locally. A live campaign replay is still required to accept the generated dialogue, final rescue acknowledgement and grid experience. The running application and saved campaigns were not changed during this work.

Baseline: `dev` at `f77f4e3c2388afc4d42c863704a897a9121905e8`. Implementation branch: `codex/campaign-replay-readiness`.

## Changes

| Priority | Implemented behavior | Verification and limit |
| --- | --- | --- |
| NPC identity and rescue acknowledgement | Reviewed descriptions of visible or previously encountered NPCs survive planner context trimming. Observation validation restricts registry citations to those exact description spans. Narration and planning explicitly prioritize completed rescue/safety facts over opening staging. | Compiled-source regressions cover canonical Jory identity, multiple actors in a different registry order, retained encounter facts and tight context budgets. Private motives, knowledge and unmet actors remain ineligible. Prompt guidance still needs real-model campaign acceptance. |
| Preparation repair and recovery | Retained page corrections preserve known physical pages even when the paid candidate omits them. Catalogue quotes tolerate whitespace and equivalent distance abbreviations while preserving numbers and rule words. A validation-policy marker permits the outer repair to recheck its retained candidate. Ambiguous provider dispatches block ordinary retry before any run-state change. Public status distinguishes correction failures from missing receipts and reports durable retained work. | The actual retained Wild Sheep checkpoint was copied, repaired and fully structurally validated without a model call. All task receipts remain identical to the live checkpoint; a second repair is a no-op. The live preparation remains held. This proves structural recovery, not final review/publication. |
| Surrender, custody and pickup | A foe’s trusted canonical identity can bind source surrender terms while its public label remains the displayed name. Natural handover/ceasefire offers enter the surrender flow. A failed optional inspection no longer blocks a separately declared, available ordinary pickup. Narration may claim acquisition only when an accepted consequence records it. Present-captive dialogue guidance distinguishes speaking from taking custody. | Regressions cover the publicly unnamed half-orc/private Guz binding, confirmation, no combat roll for an accepted offer, visible-object acquisition after failed Arcana, and refusal of failed theft/retrieval, negated pickup and quoted instructions. Existing inventory persistence/API coverage passes. Custody dialogue and narrative consistency need live acceptance. |
| Minimal tactical grid | The encounter panel supports creature inspection, empty-square selection, a backend path/cost preview, Dash, reaction warnings and an explicit draft button. Changed snapshots and stale responses cannot authorize an old preview. Solid walls block attacks and catalog spells; known ranges are checked before dice/resources. Prone crawling/standing and fear movement use the shared route budget. | Pure and round-level regressions plus DB/API tests cover immutability, membership, walls/corners, range refusal, crawling, standing and enemy attacks. Frontend tests and isolated Chrome checks exercise the draft flow. Grid creation remains disabled by default. |
| Trustworthy tests | Repaired retired imports, obsolete request/route contracts, incomplete prepared-source fixtures, Compose allowlist drift and prompt provenance records. Compose now forwards the optional retained rules-PDF directory and omits two unused settings. | The full backend suite passes, including disposable Postgres/Qdrant integration. The previously problematic agent-before-API combat order also passes. |

## Validation

- Full backend: **3,732 passed, 6 skipped**, 257.44 seconds. Command: `DOCKER_HOST=unix:///Users/psmaniotto/.docker/run/docker.sock uv run pytest -q --tb=short`.
- Final follow-up checks for the last route/status guards: **86 passed** (`test_campaign_recovery_v2`, `test_campaign_play_boundary`, `test_combat_grid`).
- Alternate combat test order: **1,691 passed** (`tests/agent`, combat diagnostics API, party combat API).
- Frontend: **481 passed across 75 files**; production TypeScript/Vite build and ESLint pass.
- Ruff passes for tracked Python files; `git diff --check` passes. Historical untracked playtest scripts were preserved and excluded from the tracked-source lint check.
- Isolated Chrome fixture: **1440×1000, 1280×800 and 390×844**. Each prepares `I move to A5.` with no document horizontal overflow and no campaign connection. Route responses are local fixtures; this is UI verification, separate from backend route tests and live acceptance. [Receipt](browser-fixture-verification.json), [1440 screenshot](grid-fixture-1440.png), [1280 screenshot](grid-fixture-1280.png), [narrow screenshot](grid-fixture-390.png).
- Retained checkpoint: [structural recovery receipt](retained-checkpoint-verification.json). Physical page **6** is preserved, external locator **158** remains outside physical-page provenance, and the profile selects the existing verified catalogue template. No provider submission, publication, campaign-history edit or resource reset was performed.

## Remaining acceptance boundaries

The grid remains experimental and opt-in. Area geometry, cover, authored maps, large creature footprints, Ray of Frost speed reduction, complete special movement and hero opportunity attacks remain outside its implemented subset. See [the grid contract](../../combat-grid.md).

The legacy Wild Sheep wand’s True Polymorph restoration, attunement/charges and resulting finale are still unsupported by that pinned publication. This change fixes ordinary pickup persistence; it does not add that source-specific magical procedure or migrate the existing campaign to another publication.

The fresh PDF and AI preparations still have ambiguous paid dispatches without known response IDs. Their retained work is now reported accurately and ordinary retry is blocked. Recover and reconcile the exact provider response IDs before resuming them. The separately retained Wild Sheep preparation has a verified structural repair path, but still needs the normal remaining review and publication stages.

## Next campaign attempt

1. Deploy the committed `dev` revision to the local application, preserving the existing database, vector store and campaign mount. Apply pending migration `24_combat_battlefield` before using grid encounters. Leave grid creation off for the first narrative regression pass.
2. Replay the ferry’s rescue/return/resolution: Jory must retain his ferryman identity, Mara must acknowledge the completed rescue, and securing the barge must stay resolved.
3. Exercise a natural peaceful handover during half-orc combat, dialogue with present Finethir while Guz holds custody, and pickup/reload of the visible wand after an optional failed inspection. Narration and persisted state must agree.
4. Resume the structurally repaired Wild Sheep preparation through its ordinary remaining review/publication path. Keep the two ambiguous fresh jobs held until their receipts are reconciled.
5. Opt in a separate newly opened grid encounter and exercise inspection, route previews, Dash, wall/range refusals, prone movement and reload. Existing encounters retain their persisted positioning model.
