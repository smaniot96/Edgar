# Whole-adventure review contracts (EDG-020)

`edgar_core.coherence` provides private preparation contracts for P09 whole-manuscript
review, P10 path exploration and bounded P11 repair validation. These contracts build on
the existing `AdventureManifest`, `PreparedCampaign`, artifact and supervisor records. They
do not create a second canonical story graph.

## P09 inputs and outputs

P09 proposes exactly one final `canonical_manifest`, `prepared_campaign` and
`retained_manuscript` artifact, plus one `deterministic_review_report` and one
`semantic_critic_report`. Their payloads use `manifest`, `prepared`, `manuscript`,
`review_report` and `critic_report` keys respectively. The two code validators resolve the
three review subjects from the current task's proposed artifacts, never from an older
accepted version. They bind the final subjects together with the task's complete accepted
dependency inventory, including the one `grounding_report`.

Use `build_review_bindings()` when preparing the independent critic request. It returns the
run, source, hero, profile, final artifact IDs/digests and exact input inventory that the
critic must echo. `RetainedManuscript` holds complete ordered chapter text, stable scene and
advance references, structured continuity assertions and chronology events. Chapter and
scene coverage, prepared-campaign structure, identity references and chronology are checked
deterministically. Continuity assertions mark stable facts separately from chapter-local
state, so a changed motive or learned fact is not automatically treated as a contradiction.
The semantic critic must return a SHA-256 for every full retained chapter
and each finding must include an exact artifact digest, JSON pointer, excerpt and excerpt
digest. A critic verdict never resolves findings or approves publication.

Each P09 validator returns a supervisor receipt whose `artifact_digests` covers **all**
proposed artifacts and whose `dependency_digests` covers the accepted task dependencies.
The P09 task builder should therefore build the report artifact with the task's accepted
dependencies, while including the final three candidate artifacts in the report's
`ReviewBindings`. The deterministic and critic validators both inspect the same complete
proposal.

## P10 path exploration

`explore_prepared_paths()` performs a bounded breadth-first traversal from the reviewed
opening. Scene movement uses `select_scene_context()` and its WorldFlag-gated canonical
links; policy and outcome predicates and item prerequisites are checked before applying
authored effects. Discovery effects set only the canonical entity discovery flag. Move
effects can target only scenes available from the current runtime context. The explorer tests
authored choices and does not claim to enumerate arbitrary player language.

`build_path_exploration_artifact()` creates a P10 `path_exploration_report` from the latest
accepted manifest, prepared campaign and retained manuscript. Register
`make_path_exploration_validator()` as the P10 code validator. Its passing receipt covers
every P10 proposed artifact. If a state or depth limit is reached, unexplored outcomes are
reported as unverified; they are not mislabeled unreachable. Only a complete traversal can
declare an authored outcome unreachable.

## P11 immutability checks

`validate_repair_envelope()` checks that a repair round targets exactly the selected finding
and artifact IDs, creates one newer P11 artifact of the same kind for each target, and
preserves the frozen run/source/hero/profile bindings. Retained source and grounding
artifacts cannot be replaced. The caller supplies the sealed revealed-fact digest inventory;
the envelope must preserve it exactly. Repaired artifacts still need their ordinary P09/P10
validators before findings may be resolved.

These checks validate declared identities and evidence snapshots. They do not generate
manuscripts, call a provider, persist a repair round, infer revealed facts from prose, or
prove semantic correctness. The root-owned P09/P10 stage adapters remain responsible for
building the final task proposal and using the configured provider seam for the independent
critic.
