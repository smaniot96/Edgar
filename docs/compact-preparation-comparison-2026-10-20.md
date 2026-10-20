# Phandelver compact preparation comparison — 20 October 2026

The owner requested a lighter preparation path after stopping the original P04
authoring run. Ten minutes and USD 2 are comparison targets, measured afterward;
neither is an execution limit. No CI, test suite, or remote push was run.

## Preserved baseline

- Source: `lost_mine_of_phandelver_edg_055_rerun_3`.
- Original run: `prep-5d81406d90104c45b253c652a44ceee0`, cancelled.
- Accepted source/book studies and design: 21 artifacts, eight acts, 50 scenes.
- No compiled act was accepted; complete unaccepted first-act proposals and
  their corrections remain available for a quality comparison.
- Final recovery snapshot: `data/recovery-snapshots/phandelver-rerun-3-2026-10-20/campaign-work-stopped-final.tar.gz`
  in the owner's original workspace. All 39 archived files were individually
  checked against their original hashes before the optimization work.
- Snapshot SHA-256: `d63e16445ff8111aef6762a922f09999b9cb5b189a2f810395a5fbd3111bcb7b`.
- The last old correction was submitted at 14:21:32 Rome, before cancellation at
  14:21:43, and finished at 14:22:28. Local cancellation previously fenced the
  worker without cancelling its OpenAI background response. The new response
  wrapper cancels that request when the durable user cancellation is detected,
  retaining returned output and usage. Worker restarts keep their existing jobs.

## Compact implementation and live comparison

Run `prep-95ea542c81af43ec9dea3f61a77e9f85` imports the original P01–P03
payloads into fresh artifact identities. Source, book and design checks run
locally again. A lineage artifact records original digests; the original
checkpoint and proposals are never rewritten. Imported work is excluded from
the fresh usage counters.

The first compact authoring response is
`resp_0dbbbbb49ca021a2006ad762312f0c87d29340a62ec905cafe`, submitted at
14:44:33 Rome. Completion, fresh cost, elapsed time and quality results are still
pending at the time this section was written.

The model writes one semantic blueprint for the whole campaign: concise scene
notes, important shared facts, routes, grounded creature requests, and meaningful
decisions with explicit consequences. Code expands IDs, provenance, policies,
branches, outcomes, packets and references. Every exact source character remains
classified, with unselected source private. Smart thematic additions are marked
as DM extensions. Printed facts, important discoveries and ownership are retained.

Mechanics grounding, scene compilation, chapter assembly, topology/handout
construction, graph traversal, seal validation and publication remain real local
operations. One independent review receives every complete chapter, authored
blueprint, public disclosure, canonical effect/route and grounding evidence.
The complete artifact inventory is still bound by hashes and original evidence
pointers. A model answer alone does not make the campaign Ready.

Repairs apply individual fields/records to saved paid proposals. A material review
finding retains P03 and the blueprint, then recompiles and revalidates downstream
records. It does not redo source studies or the accepted design. Repair-count
estimates are advisory. A correction that leaves both the proposal and diagnosed
defect identical is reported as no progress instead of repeatedly paying for it.

Fresh estimates use the existing accounting rates of USD 4/M input tokens and
USD 20/M output tokens. These are accounting estimates, not an OpenAI invoice;
provider usage receipts are retained for reconciliation.

## Validation performed

Python compilation/import checks and local production revalidation of the
retained source/book/design artifacts passed before dispatch. No model call was
used to repeat studies or design. Runtime quality acceptance and the comparison
result remain pending until the compact run actually finishes.
