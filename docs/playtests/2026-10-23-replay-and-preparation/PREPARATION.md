# PDF and AI preparation acceptance

The deployed preparation implementation is `b36628ae80c9847714fe4cdc006f40e8c48c07aa`. The final full backend run passed **3,749 tests with 6 skipped**; all **365 preparation tests** also passed independently.

Public status captured at **2026-10-23T11:32:05.055Z** is recorded in [preparation-status.json](preparation-status.json). Both active jobs remain protected from play until their normal review and publication gates complete.

| Active preparation | Latest observed progress | Remaining boundary |
| --- | --- | --- |
| Retained Wild Sheep PDF | Structural corrections, field consolidation/rebase and subsequent capability review passed. The next scoped scene correction is processing at the provider. | The aftermath opening must preserve whether immediate danger remains; independent review and publication have not finished. This preparation is not Ready. |
| Lanterns at Greybank AI | Premise, cast, content contract and outline accepted; first chapter has its exact saved response ID. | The provider still reports the chapter as queued. No chapter has been accepted, and this preparation is not Ready. |

The [retention receipt](preparation-retention-receipt.json) confirms all nineteen calls present before the final PDF recovery and all five calls in the AI job retain their original response IDs. The PDF still has six physical source pages. The application continues polling the two active saved responses after this report; there is no duplicate create or forced publication.

The retained Wild Sheep PDF preparation resumed through the normal UI. Its original six physical source pages and all eight original provider response IDs were preserved. It passed its earlier structural correction and reached independent review. Review identified two scene-viewpoint defects; normal scoped repairs and re-review addressed them. No publication or fidelity gate was bypassed.

Live review exposed two defects now fixed in `dev`: authored check IDs were incorrectly rejected because their records are nested in chapter content, and a later model response copied three long capability hashes incorrectly. Exact check IDs now resolve to their containing authored records. The coverage recovery keeps twelve unique, exact, valid decisions and reviews the remaining three gaps using short selectors whose exact hash bindings are held by the application. A malformed identifier never counts as a reviewed capability. This retained review reached all fifteen exact decisions. Three material capability findings then prompted the normal scoped prose repair: door checks do not commit a door-opening result, Noke's authored spell tactics are unsupported, and the wand's transformation/restoration/malfunction procedure is not executable.

That retained repair proposed overlapping substitutions in the wand field and an absent quotation in Noke's field. `891d6f8` adds one focused consolidation per overlapping record/field; `b36628a` adds a bounded rebase for absent or ambiguous quotations. Each receives the actual accepted field and retained proposed corrections, preserves unrelated edits, and still requires exact atomic validation and independent review. There is no fuzzy substitution or automatic retargeting. In the live job, both corrections were accepted, the original scoped-repair receipt was reused, and preparation reached independent review again. No create request was repeated for that retained repair. The next independent capability review passed. Its scene critic identified one remaining safety assumption in the aftermath opening, which entered the normal scoped-repair workflow.

New provider dispatches save their background response ID from the first streamed event, before waiting for full output. They send a stable client request ID and retain the frozen original semantic request. The PDF's new calls demonstrated receipt persistence before completion. The separate small AI adventure, **Lanterns at Greybank — AI Preparation 20261023**, accepted its premise, cast, content contract and outline. Its chapter response ID was saved while queued. During the API deployment, startup recovery continued that same receipt; the five existing call identities and response IDs were preserved. This was a separate village-lantern mystery, not a replacement submission of the ambiguous Crooked Ferry request. A later direct retrieval of its exact chapter response returned `queued` with no provider error in 0.556 seconds. The original request remains attached to the job and the worker continues polling it; preparation is not Ready, and no duplicate create was submitted.

The two older 22 October preparations remain held:

| Adventure | Retained work | Recovery boundary |
| --- | --- | --- |
| `the_wild_sheep_chase_fresh_acceptance_20261022_1520z` | PDF source and accepted outline | The chapter dispatch may have completed remotely, but its exact response ID was never saved. Ordinary retry is disabled. |
| `the_crooked_ferry_acceptance_20261022_1520z` | Accepted outline and three chapters | The fourth chapter dispatch has the same missing-receipt problem. Ordinary retry is disabled. |

Their public failure is `provider_receipt_missing`, with `outcome=unknown` and `retryable=false`. Both checkpoints are intact. No create request was repeated for either ambiguous dispatch. A private recovery packet records the exact dispatch hashes, UTC submission times and neighboring known response IDs at `/Users/psmaniotto/EdgarBackups/replay-acceptance-20261023/private-ambiguous-recovery-packet.json`. The local operator tool can retrieve an exact response ID but cannot search remote responses by metadata; recovering those two IDs requires provider-side request records. No external support message was sent.

After an exact matching response ID is found, use the documented reconciliation command and then resume through the normal UI. Do not clear dispatch rows, guess response IDs, change the frozen request, or resubmit the same work under a new slug. See [the provider recovery procedure](../../campaign-provider-recovery.md).

Preparation readiness does not add execution support for the legacy Wild Sheep campaign's wand charges, attunement, transformation or restoration. The replay's successful pickup proves custody only. No complete restoration finale was accepted in this work.
