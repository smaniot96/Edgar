# Preparation reliability audit — 20 October 2026

Requested by the owner while Phandelver rerun 3 was running. The subagent reviewed
`1370146` and then reviewed the local follow-up diff. Review was read-only: no
tests, CI, model calls, service restarts, or campaign mutations by the reviewer.

## Live campaign at audit time

Run `prep-5d81406d90104c45b253c652a44ceee0`, source
`lost_mine_of_phandelver_edg_055_rerun_3`.

P03 completed and its design was accepted: 8 acts, 50 scenes. P04's first act is
being compiled by saved background response
`resp_01c39c17df619708006ad7435815dc87d2a25f91e05d8f3801`.
The campaign is not Ready. The original P01/P02 inventory and accepted design
must be preserved. The active provider job is not a reason to restart preparation.

## Findings addressed in the local follow-up

1. **Daily price cap could still block preparation and play.**
   `apps/api/limits.py::_reserve_daily_spend` rejected against the $50 daily
   default and failed closed during Redis outages. It now records estimates and
   logs warnings without monetary rejection; unavailable accounting fails open.
   An advisory estimate no longer prevents startup when a job estimate is larger.

2. **Every retry reserved the entire campaign estimate again.**
   `apps/api/routers/adventures.py::admit_retry_job` no longer makes this repeat
   reservation. Existing historical reservations are not rewritten or presented
   as actual provider spending.

3. **Rejected/failed responses could be replayed forever.**
   `preparation_responses.py` now binds request identity to its logical subcall,
   archives rejected/terminal generations, and gives a replacement a durable
   dispatch identity. Interrupted in-flight jobs retain their exact response ID.
   Receipts include their dispatch key so a crash between dispatch/receipt writes
   cannot mistake an older rejected receipt for a newer submitted generation.
   Recorded response usage is deduplicated by response ID across task attempts.

4. **P09 feedback could mix chapters or miss schema feedback entirely.**
   `preparation_proposals.py` scopes feedback to the logical subcall. The stage
   wrapper uses the actual planned subcall task consistently, including changed
   per-call estimates. Historical feedback is reused only for single-subcall
   stages; old ambiguous P09 feedback is not injected into another subject.

5. **Profile output allowance could truncate a larger task.**
   `preparation_provider.py` no longer sends an optional application
   `max_output_tokens` allowance. The provider's actual supported context/output
   capacity still applies. This is not a guarantee that an arbitrarily large
   answer can fit a single generation.

6. **Non-proposal failures lost their diagnostic detail.**
   Planning, final stage validation and publication now retain exact exception
   details and dependency digests privately beside the checkpoint. Public API
   projections remain sanitized.

## Remaining findings requiring further implementation

1. **P04 has no shared canonical registry.** Each act's frozen dependencies
   precede P04, although later acts must reference earlier shared entity, policy,
   discovery and object IDs. Whole-graph validation occurs before the first P06
   proposal, where the ordinary correction loop has no rejected proposal to amend.
   Supply pinned shared definitions and validate/repair the assembled boundary
   before downstream spending, preserving unaffected fragments.

2. **P11 repair rewinds all P03-and-later work and stops after two rounds.**
   `_restart_for_repair` in `preparation.py` archives but removes accepted design,
   canonical compilation, mechanics, scenes, prose, assets and reviews. Its
   `max_repair_rounds` is still a hard stop. Replace this with repairs to affected
   definitions/acts and their actual dependent closure; round counts should be
   diagnostic. Simply deleting the round check would permit unlimited full
   rebuilds and does not meet the owner's preservation requirement.

3. **P09 input grows with the whole manuscript in every act review.**
   It includes complete prose, prepared graph, grounding and guidance. Split
   chapter review from cross-act continuity review while retaining exact evidence;
   do not substitute another spending threshold.

4. **Exact source-offset counting remains fragile.** P04 asks a model to count
   character spans across whole physical pages. Supply deterministic segment
   boundaries or quote anchors and derive offsets in code. Keep exact coverage,
   audience/disclosure and provenance checks.

5. **Unknown historical reservations are not actual response usage.**
   Response-ID deduplication prevents repeating known charges. Earlier interrupted
   attempts with no saved response identity can still retain an unknown reservation
   plus the eventual known usage. These must remain distinguished in reports;
   reconciling them requires binding reservations to durable dispatch identities.

6. **Optional images still have call/price allowances and HTTP deadlines.**
   `adventure_assets.py` retains these. Current commissions force generated art
   optional, so optional image failure should not prevent campaign completion.

## Deployment and validation

The follow-up patch is prepared locally and reviewed by inspection. It has not
been deployed over the active P04 generation, pushed, or run through tests/CI.
This respects the owner's request to resume without CI/testing and avoids an
unnecessary API restart while the current background job is producing work.

The earlier background-Responses change already removed the overall generation
deadline. The 300-second profile value now limits individual HTTP operations;
temporary polling failures continue retrieving the saved job. Source integrity,
exact citations, secrecy, supported mechanics and publication validation remain
acceptance requirements.


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
