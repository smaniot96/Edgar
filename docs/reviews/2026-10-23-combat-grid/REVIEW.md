# Combat-grid review and integration — 23 October 2026

**Useful as an experimental backend foundation. Integrated with the current combat fixes, disabled by default. It is not ready to replace the game's encounter view.**

The campaign assessment was completed first: [resumed acceptance report](../../playtests/2026-10-23-resumed-acceptance/REPORT.md). Its tested deployment remains `02487e92c056d9a142e4e51b1239bff13c209737`; this integration was not deployed or played against retained campaigns.

## Original work and preservation

Reviewed `/Users/psmaniotto/.claude-worktrees/edgar-b3/combat-grid`, branch `codex/combat-grid`, based on `80028b7203c1e57d85c8b23f6bd9d4bb77799c8a`. That commit is already an ancestor of current dev; the useful work was entirely uncommitted: **13 modified tracked files and four new files**. A binary patch, archive and SHA256 inventory were saved before integration. All17 original files remain byte-identical. The original checkout, branch, index and uncommitted work were preserved.

Integration started from current dev `02487e9` in a separate managed worktree. Six overlapping files required conflict resolution. The generated frontend schema was regenerated from the merged backend instead of accepting either old generated copy. The nullable migration follows the current head23 without creating another migration head.

Local raw review evidence remains in this dated directory: `original-work.patch`, `original-files.tar.gz`, individual test/build/lint logs, and baseline comparison logs. The committed [validation summary](validation-summary.json) records their hashes and outcomes; the original [source inventory](source-inventory.json) and [preservation receipt](source-preservation.json) record exact provenance. Large raw files are retained locally rather than added to the repository.

## What is worth retaining

- Engine-owned five-foot squares, deterministic battlefield generation, occupancy and route planning with movement costs.
- Persisted terrain and creature positions; API/stream fields for battlefield, square labels and speed, plus matching TypeScript types.
- Weapon reach and normal/long range, long-range disadvantage, route-based enemy opportunity attacks, enemy movement and Dash.
- A band projection that lets existing combat consumers continue to operate, while encounters without a battlefield keep their old abstraction.
- Original geometry/combat tests, expanded with integration regressions and a real Postgres insert/update/fresh-session reload test.

## Findings addressed before landing

| Finding | Integration change |
| --- | --- |
| Grid enabled for every new fight despite no grid UI and incomplete rules | Added **EDGAR_COMBAT_GRID=0** by default in shared config, environment example and Compose forwarding. Only an explicit1 opts newly opened fights in. Persisted grid encounters retain grid rules after restart or disabling the flag. |
| Original branch predates ordered declarations and event feedback | Retained current execution ordering, before-dice warning behavior, event history, multiattack metadata and applied-healing behavior while integrating grid routing. |
| Square moves were assigned text position0 | Square moves now participate in operation ordering. `disengage and move to A5` applies Disengage first; `attack then move to A5` attacks before withdrawal and its opportunity attack. |
| Only the first square destination was read; quoted/negated square moves could authorize movement | Parse all affirmative square moves, share the route budget, and clarify movement split around an action. Quoted, hypothetical and negated moves authorize none. |
| Endpoint-only fear check allowed an approach followed by retreat | Inspect route steps for approaching a fear source, before dice and again at execution. |
| Grid speed ignored immobilizing conditions | Grappled, restrained, paralyzed and stunned creatures have zero movement. |
| Reach weapon granted its range to grapples; runtime band guard blocked legal10-foot weapon attacks | Maneuvers use five-foot reach, weapon attacks use recorded weapon reach, and runtime range validation uses grid distance. |
| Opportunity trigger used longest reach but reaction could choose a shorter attack | The grid reaction uses the corresponding longest-reach melee attack. Full choice between different reach triggers remains outside this subset. |
| New movement bypassed current round-event history | Hero and enemy grid movements now record ordered movement events. |
| A single stated ranged distance was not parsed | Accept both `range150/600 ft.` and `range150 ft.`; preserve values through monster serialization. |
| Global supported-mechanics entry claimed a playable grid and clickable movement | Kept the default supported-rules contract. Documented the opt-in backend separately in [combat-grid.md](../../combat-grid.md). No general preparation capability claim is added. |
| Current golden setup fixture lacked new attack metadata | Intentionally updated only four additive fields: Bite reach5/range null and Claw reach10/range null. Mechanics, narration, dice and the other golden scenarios remain unchanged. |

## Validation and inherited suite failures

No paid provider calls or live campaign mutations were needed for integration tests. Database tests used disposable Postgres containers.

| Check | Result |
| --- | --- |
| Grid and backend stream-contract tests | **55 passed**, including all **38 grid cases**. |
| Broad affected backend selection | **1,740 passed, nine failed, one skipped**. All1,647 agent cases pass. Failures are the same nine order-sensitive combat API failures reproduced on unchanged dev. |
| Same broad selection on unchanged dev | **1,701 passed, the same nine failed, one skipped**. The candidate adds38 grid cases and one persistence case; all39 additions pass. |
| Diagnostics and party API files in isolation | **20 passed on candidate and20 passed on unchanged dev**. They expose an existing cross-test-order dependency in the broad selection. |
| API stream/confirmation/projection, persistence and migration selection | **65 passed, one skipped**. Includes schema drift and upgrade/downgrade checks against temporary data. |
| Frontend | **479 passed /74 files**; production TypeScript/Vite build and ESLint pass. |
| Static/schema checks | Ruff, generated stream schema freshness and `git diff --check` pass. Alembic has one head: **24_combat_battlefield**. |

The initial focused run found the Disengage ordering issue (26 passed/one failed); it was fixed. The first broad agent run found one golden metadata mismatch (1,654 passed/one failed); the inspected metadata-only fixture update resolves it. Initial frontend contract tests found missing key-set entries (477 passed/two failed); the TypeScript types and exhaustive contract key sets now agree.

An attempted full backend run was interrupted after **1,872 passed, three failed and nine setup errors** when unrelated stale fixtures began failing. The three failures concern campaign-source request expectations and route classification; the setup errors reference the removed `apps.api.services.preparation_commission` module. A baseline run on unchanged dev reproduces those three failures and the same generation setup error (stopped at four problems). **The full backend suite is not claimed green or complete.** The separately completed broad selection and baseline comparison establish no new failures in the assessed combat changes.

The nine broad-selection failures are two cases in `test_combat_diagnostics_api.py` and seven in `test_party_combat_api.py`; exact test names are in the validation summary and raw logs. Fixing those pre-existing test-order dependencies and stale preparation/API fixtures is separate work.

## Readiness boundary

There is no interactive tactical grid frontend. Terrain is synthetic scenery inferred from scene words, not a source-authored map. Cover, line of sight, footprints, area templates, spell/save-action distance, complete prone/speed rules, forced movement, full grapple separation and complete fear-aware enemy routing remain incomplete. Some old boundary/declaration text still describes the default range-band engine. Those gaps make unconditional activation inappropriate.

The useful backend is available in dev behind the explicit switch, with persisted old encounters left on their existing rules. See [combat-grid.md](../../combat-grid.md) for activation, API fields and the complete limits. No deployment, database migration against the running game, remote push, campaign reset or user-source edit was performed as part of landing this review.
