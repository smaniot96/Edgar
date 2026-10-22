# Prompt experiments (EDG-059)

An offline framework to version the play prompts, describe scenarios declaratively, record what a run
used, and compare two runs. It runs with scripted fake models only. Nothing here calls a provider, and no
live run has been performed or authorized.

## What it does and does not do

- Does: gives each play prompt a stable id, semantic version and sha256; fails when a prompt's text changes
  without a version bump; validates versioned scenario files; runs them through scripted outputs and the
  production deterministic guards; writes one JSONL run file; compares two run files.
- Does not: judge model quality, score fun automatically, call any model, or change runtime prompts,
  profiles or behaviour. Scripted outputs test the checkers and guards on known text. They say nothing
  about what a real model would write.

## Layout

| Path | Role |
| --- | --- |
| `apps/agent/src/agent/prompt_registry.py` | Prompt ids, versions, sha256, `prompt_manifest()`, `manifest_drift()`. Points at the runtime constants; it owns no copies of the text. |
| `evals/prompt_manifest.golden.json` | Committed manifest that the bump test compares against. |
| `evals/scenarios/*.json` | Versioned scenarios (JSON, validated by hand). |
| `evals/fun_rubric.json` | Human fun rubric (`fun-v1`). Scores are recorded by a person only. |
| `tools/experiments/scenarios.py` | Scenario schema and loader. |
| `tools/experiments/checks.py` | Deterministic checks (`CHECKER_VERSION`). |
| `tools/experiments/runner.py` | Offline runner, run-record writer, live guard, CLI. |
| `tools/compare_runs.py` | Compares two run files. Standard library only. |
| `tests/test_prompt_registry.py`, `tests/test_prompt_experiments.py`, `tests/test_compare_runs.py` | Offline tests. |

Prompt ids are the model-profile stage names: `play.parser`, `play.consequence_planner` (the rules
adjudicator), `play.narrator`, `play.combat_builder`, `play.combat_narrator`, `play.memory_summarizer`
and `play.session_intro`.
`play.combat_narrator` sends the same text as `play.narrator` (`agent.nodes.narrator`), so both entries share
a hash and a bump to `NARRATOR` changes both. The opening service imports `SESSION_INTRO` from
`agent.prompts`, so the registered opening text is also the exact runtime constant. Campaign
generation and preparation prompts are not in this registry.

## Prompt versions

Every entry has `version` (semver) and `sha256` of its exact UTF-8 text. The rule is:

1. If you change a prompt's text, bump its `version` in `prompt_registry.py` in the same change.
2. Refresh the golden manifest:

   ```bash
   .venv/bin/python -m tools.experiments.runner manifest --write
   ```

3. Check it: `.venv/bin/python -m tools.experiments.runner manifest` (exit 0 when it matches) and
   `.venv/bin/python -m pytest tests/test_prompt_registry.py`.

`tests/test_prompt_registry.py` fails with a "text changed but version is still X" message when the text
changes and the version does not. It also fails when the golden file is stale. The test does not check that
the bump is the right size; choose the semver level yourself.

To add a prompt: add a `PromptEntry` to `PROMPTS`, refresh the golden manifest, and extend
`EXPECTED_IDS` in `tests/test_prompt_registry.py`.

## Scenarios

A scenario is one JSON file in `evals/scenarios/`. Required keys: `id`, `version`, `title`, `player_input`,
`character`, `engine`, `scripted`, `agency`. Optional: `description`, `combatants`, `expect`, `labels`,
`fun_rubric_id`. Unknown keys are rejected.

- `engine`: `roll_mode`, `effects`, `check_rolled`, `success` (true, false or null), `hp_after` (name to HP).
  These are the facts the engine would own: the framework does not compute rolls or HP.
- `combatants`: roster entries (`name`, `is_player`, `hp_current`, `disposition`), read by the roster check.
- `scripted`: exactly three entries, `play.parser` (a `ParsedInput` object, validated against the model),
  `play.consequence_planner` (`mechanical_summary`, `success`) and `play.narrator` (text).
- `agency`: `expected_intent` and `must_mention` (terms the final narration must contain).
- `expect`: optional map from check id to the result you expect. A `false` value marks a deliberate
  negative control. The runner reports `as_designed` per check.

Versioning: bump `version` (semver) when anything that changes the measurement changes: the input, the
state, the scripted outputs, or the expectations. The run id is derived from scenario versions, not from
their content, so a content edit without a bump is invisible to the comparison. Do not do that.

Shipped scenarios (all v1.0.0):

| Id | Exercises | Expected |
| --- | --- | --- |
| `combat-hit-engine-damage` | roster, claims, summary agreement, engine roll line | all pass |
| `combat-revived-foe-control` | roster (negative control) | `fidelity.roster` fails |
| `combat-two-foes-one-down` | roster name-prefix guard ("goblin" vs "goblin archer") | all pass |
| `rp-persuade-captain` | failed social check is not softened; agency | all pass |
| `exploration-search-chest` | perception outcome narrated, roll line kept | all pass |
| `spell-unsupported-no-claim` | unsupported spell narrated as an attempt only | all pass |
| `spell-unsupported-claim-control` | unsupported spell narrated as working (negative control) | `fidelity.claimed_effects` fails |
| `combat-hp-agrees-engine` | HP figure in prose matches the engine | all pass |

Checks (`tools/experiments/checks.py`):

- `agency.intent_parsed`: parsed intent equals `expected_intent`.
- `agency.target_acknowledged`: every `must_mention` term is in the final narration. A sentence the
  narration guard removed does not count.
- `agency.consequence_tied`: when the engine rolled, the final summary contains an engine roll line.
- `fidelity.roster`: `roster_violations` on the raw narration is empty (no revived or invented attacker).
- `fidelity.claimed_effects`: the narration guard removed no sentence from the raw narration.
- `fidelity.hp_numbers`: every "N HP" / "N hit points" in the raw narration equals a recorded HP.
- `fidelity.mechanics_summary`: the planner's success equals the engine's, and every engine roll line
  survives the summary guard.

Fun is not a check. It is the rubric in `evals/fun_rubric.json`, recorded per scenario with `status`
`pending_human`, `reviewer` and `scores`.

## Running offline

```bash
DOCKER_HOST=unix:///nonexistent.sock .venv/bin/python -m tools.experiments.runner validate
DOCKER_HOST=unix:///nonexistent.sock .venv/bin/python -m tools.experiments.runner run \
  --out "$HOME/edgar-experiments/runs" --run-date 2026-10-17
```

Optional: `--dm-settings FILE` (JSON: `label`, `source` = `scripted-fake` or `live`, `stages` with `profile`,
`model`, `reasoning_effort`), `--human-reviewer ID`, `--allow-in-repo` (only if you really want records in
the checkout). By default `--out` must be outside the repository. A run file is never overwritten; choose
another label or date. The command exits 1 when a check's result differs from its `expect` value.

A run file is JSONL: a `run` header, then one `scenario` line per scenario. The header holds:

- `dm`: the settings of the model that plays the DM (label, source, per-stage profile, model, effort).
- `evaluator`: the checker (`checker_version`), `fun_scoring: human-only` and the `human_reviewer` id.
  This block is separate from `dm`, so an evaluator (for example a stronger model, or a person) is never
  mistaken for the model under test.
- `prompt_manifest`, `app_git_sha`, `run_date`, `profile_config_version`, `scenarios` (id to version),
  `provider_calls` (always 0 for an offline run).

The `run_id` is `<dm label>-<12 hex>`, a hash of DM settings, evaluator, manifest, scenario versions, date
and git sha. The same inputs give the same id.

## Comparing two runs

```bash
.venv/bin/python tools/compare_runs.py RUN_A.jsonl RUN_B.jsonl [--json] [--allow-mismatch]
```

It prints, per scenario and category, the deterministic pass count for each run (`agency`, `fidelity`), the
human fun means where both runs have scores, and the differences in prompt manifest, DM settings,
evaluator settings and run metadata. It refuses (exit 2) when a scenario version differs. With
`--allow-mismatch` it compares the rest and lists the mismatched scenarios as not comparable.

Every report states n and that no significance is claimed. With a few scenarios, any difference is
anecdotal.

## Human fun scoring

Only a named person scores fun. The runner collects no scores. To record one, copy a run file, edit the
`fun` block of the scenario line (`reviewer` and `scores`, one value per rubric dimension, 1 to 5), and keep
the original unchanged. Do not put model scores in `scores`. A future model-based fun score needs its own
explicit authorization and its own evaluator block.

## Live runs (not implemented, not authorized)

`runner run --live` is refused unless both the flag and `EDGAR_PROMPT_EXPERIMENT_LIVE=authorized` are
set. Even when both are set, this change only prints the estimate and exits 3. No live path exists yet.

The estimate counts logical stage calls: one per scripted stage per scenario per repeat, so three calls per
scenario, or `3 x scenarios x repeats`. Retries, the other nodes of the turn graph and any combat rounds are
not counted, so a real run will cost more. Each call is paid model usage.

Before any live run: a person must approve the model profiles, the scenario set, the repeat count and the
estimated spend in chat, and the live path must be added in a separate, reviewed change. Live results
must record their DM settings with `source: live` and the real profile names and effort, and must not be
merged with scripted runs in a comparison without saying so.

## Honest limits

- Eight scenarios. Any comparison is anecdotal. Two of them are negative controls by design.
- The checks are deterministic heuristics. The roster check is a name and verb pattern, the claim check
  uses the guard's sentence vocabulary, and the agency checks are keyword containment. They can miss
  paraphrases and can flag honest prose.
- Scripted outputs are written by hand. A passing scenario shows that the guards handle that text, not that
  a model writes it.
- Fun is unmeasured. The rubric exists, but no score has been recorded.
- The labels and expectations were written by one author with no second rater.
- Changing a scenario's content without bumping its version is not detected by the run id.
- The combat builder and the summarizer are versioned but no scenario exercises them. Session intro and
  campaign generation prompts are not versioned at all.
- `prompt_registry.py` imports `agent.graph_combat` for the combat builder text. A refactor that moves
  `ENCOUNTER_BUILDER` breaks the import loudly; the registry should then be updated to the new location.
