# EDG-140 private prepared-campaign contract

This document describes the shared, version-bound backbone contract implemented for PDF
and AI campaign preparation. It is an internal producer/consumer interface, not a claim
that either authoring producer or the full EDG-140 workflow is complete.

## Ownership and binding

`AdventureManifest` remains the canonical home for reviewed scene, entity, passage,
objective, milestone, policy and source identities. `PreparedCampaign` is an optional,
private additive field on that manifest. Prepared records refer to canonical IDs instead
of creating a second story ledger, and source passages continue to carry their original
page and span. A candidate binds to `source_mode` (`pdf` or `ai`) and the enclosing
manifest's retained source checksum, extraction checksum and rules edition. The producer
must also bind its run, hero/profile and upstream artifacts to its own checkpoints; those
bindings are outside this serializer and are validated by their owning workflow.

The bundle contains ordered acts; quests attached one-to-one to reviewed objectives and
milestones; one runnable scene packet per reviewed scene; route classifications for every
reviewed route; provenance-bearing DM extensions; materially different branches; and
policy-backed success, partial or failure outcomes. It may retain private unresolved
questions for recovery, but any nonempty list prevents deterministic publication.
`PreparedProvenance.origin` distinguishes source-authored structure from DM-created
material. Source records cite hashed, playable source passages. A DM extension has a
separate extension and passage identity, private rationale, and no claimed PDF page or
source span.

## Validation and publication

`validate_prepared_campaign` returns code-owned findings and a canonical prepared digest.
It checks typed schema, identity uniqueness, complete act/quest/scene/route coverage,
references, runtime-readable flag keys, exact route predicate agreement, branch and
outcome wiring, policy selection modes, available result paths, ending effects, broad
route-graph reachability, and provenance. It rejects cross-wired outcomes and mutually
contradictory quest, branch, outcome and selected-policy predicates. This is deterministic
structural validation; it does not establish narrative quality, source truth, hero fit,
encounter balance, or semantic consistency across prose. A separate whole-campaign critic
and the source, hero, profile and artifact checks remain required.

`require_published` applies this validation when a prepared bundle is present. The
manifest digest includes the full prepared value, so a private edit invalidates its
review receipt. When `prepared` is absent, it is omitted from the digest to preserve
historical manifest receipts. Publication still requires the existing reviewed manifest
and unconditional player-facing opening evidence.

Producers should retain the same manifest/source identity for both modes. PDF preparation
attaches the prepared candidate to the reviewed span-preserving PDF manifest. AI
preparation first retains and compiles its generated manuscript, then attaches the
candidate to that source-bound manifest. Neither producer may mark readiness from a model
claim; deterministic validation, the separate critic receipt and the existing publication
receipt refer to the exact artifact versions being sealed. P04 transport should carry the
typed `PreparedCampaign` (or schema-tagged equivalent), plus producer run and source
bindings. Runtime dependency validation remains the responsibility of the workflow owner.

## Extensions and amendments

`prepared_extension_chunks` materializes extensions separately from extracted source
chunks. Each carries `source_kind=dm_extension`, exact extension text and provenance, but
no page or physical source span. `project_prepared_extension_chunks` only returns text for
the current scene, requested audience and committed `WorldFlag` predicates. Callers must
use the prepared manifest version bound to the run and must not put private extensions into
the player view before those predicates are earned.

`validate_prepared_amendment` accepts bounded additions of branches, outcomes and
extensions against an exact prepared digest. It does not accept replacement definitions.
It checks the full amended graph, conflicts with committed flags, unchanged digests for
revealed source/extension passages, compatibility with completed outcomes, and conflicts
with supplied irreversible facts. The caller supplies the committed fact snapshot. This
function validates an amendment candidate; persistence, publication of a new version and
save migration are not implemented here. A rejected amendment leaves the bound campaign
unchanged and needs a caller-provided recovery path.

This contract does not itself implement PDF/AI producers, source-grounded encounter
compilation, semantic critic orchestration, extension retrieval/index integration,
transactional amendment publication, or the complete EDG-140 acceptance suite. The
extension chunk/projector helpers define the data boundary; integration with retrieval and
index lifecycle remains owned by the root workflow.
