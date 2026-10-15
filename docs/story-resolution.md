# Continuous history and reviewed action policies

The API reads the greatest valid summary coverage boundary and every later narration in
one database statement. Parser, planner and narrator receive that entire uncovered tail.
Delayed or failed summarization therefore increases context instead of silently omitting
intermediate turns. Committed story quotations and resolved combat outcomes override
conflicting older prose. Existing narration rows and summary publication rules are unchanged.

## Reviewed source authority

Adventure manifests can declare optional `resolution_policies`. A policy binds an action to
reviewed scenes, evidence passages, prerequisites, required inventory, an automatic result
or an existing engine check with an exact DC, and authored success/failure consequences.
The parser selects a collection-qualified policy key; it does not supply the DC or effect
payload. A selected unavailable, stale or unknown policy is rejected. Ordinary actions without
a selected policy continue through the existing engine.

Policies use the existing canonical story validator and ledger. They do not grant arbitrary
HP changes, damage, conditions, bonuses or new core mechanics. Failure progress is explicit:
an essential reveal, partial result or supported clock/object consequence must be authored
and reviewed. Source editions, source/review digests, exact retrieved passages, current scene,
prerequisites and ownership remain part of validation. Missing coupled consequences reject
the whole policy result.

Earned clue rereading uses the committed exact quotation and its validated public source.
Reviewed policy results and exact rereading share deterministic narration across sync and
SSE, so a later narrator cannot substitute a different quotation or deny an accepted reveal.
Public responses omit policy selectors and private source evidence. Existing turn receipts,
snapshot checks and the caller-owned atomic transaction govern persistence and retries.

## Compatibility and limits

An empty policy extension is omitted from the manifest digest, preserving existing reviewed
receipts. No database migration or automatic source reclassification is required. Existing
sources gain authored behavior only after explicit policy review and publication; installing
this code does not infer policies from arbitrary prose or republish sources.

The uncovered history tail is intentionally not truncated. Token-aware budgeting remains a
separate capacity requirement (EDG-075). This change does not implement full 5e mechanics,
the authoring redesign, player workspace redesign or committed-feedback UI (EDG-144).

## Evidence standard

Deterministic fake-provider tests and disposable databases/indexes establish technical
behavior, including exact DCs, quotation preservation, failure costs, alternatives, isolation,
reload and transaction rollback. They do not establish human pacing, agency or fun.
EDG-006's sustained human scenario F-T02 and UX-F03 study evidence remain open under the
full local brief, coordinated with EDG-055. Merge and deployment are separate actions.
