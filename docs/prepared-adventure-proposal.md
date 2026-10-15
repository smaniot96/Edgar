# EDG-140 proposal: a reviewed private adventure with earned player views

Owner review draft, 15 October 2026. This proposal accompanies the implemented journal
knowledge contract; it does not claim the full authoring pipeline is delivered. The
worked adventure below is invented for illustration, not approved campaign content.

## Recommended product behavior

Prepare a finite, coherent adventure with significant alternatives before publication.
Keep its definitions private. Track actual events and knowledge separately for each
playthrough, using the existing story ledger. Journal reads and reference tools need no
model call. Preserve the player's ability to take an unanticipated approach.

A useful preparation result contains stable quests, scenes, people, objects and clues;
prerequisites and reveal evidence; encounter/reward/hazard references; urgency clocks;
and compatible endings. It distinguishes source facts, unresolved questions and explicitly
reviewed extensions. It makes no promise to enumerate every possible player sentence.

## Worked example: rescuing the ferry keeper

| Private definition | Prepared alternatives | Player visibility |
| --- | --- | --- |
| Rescue the ferry keeper | Negotiate release, find a side entrance, or defeat the guards | Opening announces the rescue goal; routes appear only when learned. |
| Tower across the river | Reachable after crossing; a maintenance gate can provide another route | Opening may name the tower as known. It remains unvisited and is never called the current place. |
| Lens note | Exact source: “The lens tracks reflections, not the sky.” | Discovering the note earns that exact quotation. Dropping the item changes ownership, not learned text. |
| Reflection hint | Requires an earned discovery or authored successful inspection | An earned hint becomes searchable only after the reveal is committed. |
| Rising water | A private maximum and consequence constrain the scene | “Water is rising” can be known while the number/maximum/completion remain private. A reviewed scale-reading reveal can disclose the current value alone. |
| Brace secured | Alters the known brace/environment and may enable a route | A committed consequence links to the known brace/place; narration alone does not change it. |
| Endings | Rescue, negotiated partial success, failed rescue with a durable consequence | Only the achieved/learned outcome becomes visible. No hidden ending counts or silhouettes. |

Example opening: the player knows the rescue goal and has heard of the tower. The journal
contains that goal, the known tower and the visited ferry steps. It contains no Lens note,
reflection hint, maintenance gate or ending list. Later discoveries add their exact facts
once; another playthrough has its own knowledge.

## Player experience

Upload PDF or request AI generation → private DM preparation → ready to play. The player sees preparation progress,
source/setup problems that can be fixed without story spoilers, and the earned opening.
They never review private branches, endings, ambiguities or suggested content changes.
The DM owns complete preparation and resolves content gaps privately. The owner reviews
this system behavior; adventure-content review is an internal DM responsibility.

## PDF workflow proposal

1. Retain the original PDF/extraction and source/edition/version identity.
2. Propose canonical structure from exact page/span evidence. Record omissions, uncertain
   connections and mutually exclusive source alternatives explicitly.
3. The private DM preparation agent resolves ambiguities and authors any necessary connecting
   material. Internally retain source provenance, confidence and the distinction between PDF
   facts and DM-created extensions. Preserve exact quoted clues and published source facts.
4. An internal validation/review pass checks references, prerequisites, branch reachability,
   compatible outcomes, reveal evidence, supported encounters and safe opening seeds.
5. Publish the version-bound prepared bundle when those checks pass. Record the internal
   DM reviewer/policy and exact digest. Private edits require a new internal validation
   receipt; running saves retain their bound identity.

Recommendation: the DM owns all adventure-content decisions privately. It can retry or
repair failed preparation. A genuinely unreadable/incompatible source or unsupported
required mechanic yields a safe preparation failure and recovery action, without exposing
private content or asking the player to author/review the adventure.

## Current AI implementation, verified 15 October 2026

`apps/api/services/campaign_generator.py` currently makes one structured outline call followed
by one chapter-writing call per chapter. Sizes have 3/6/12/24 chapters, giving 4/7/13/25
successful authoring calls for a fresh attempt, before provider retries and embedding.
At most six chapters run concurrently. Checkpoints retain the outline and completed chapters;
retry can reuse them. The manuscript is retained and passed through extraction/index checks.

The outline contains premise, setting, hook, faction/NPC strings and short chapter summaries.
Each chapter writer sees that shared outline, its own summary and the chapter titles; it
does not see the other completed chapters or a validated set of canonical scene packets.
The prompt requests rough encounters for a low-level party, rather than verified encounters
for the actual hero. The generation path does not perform whole-campaign semantic review,
branch playthrough checks, source-grounded encounter compilation or automatic internal
manifest review. Existing manifest compile/edit/review/publication primitives are separate.
Successful manuscript generation/indexing therefore does not establish complete private
campaign preparation. Full EDG-140 remains Open.

## Proposed AI preparation workflow

Use explicit stages with retained artifacts and readiness checks. Multiple calls are useful
only when their inputs, outputs and validation responsibilities are clear. Calling the same
writer repeatedly without checking agreement does not establish campaign coherence.

| Stage | Private DM work and retained output | Required check before continuing |
| --- | --- | --- |
| 1. Bind the brief | Record requested tone, scope, edition, allowed books, supported mechanics and actual hero/party profile. | Inputs are available and mutually compatible. If the hero is selected later, keep encounter preparation provisional until adaptation is validated; never claim actual-hero verification early. |
| 2. Design the backbone | Create stable quests, scenes, prerequisites, significant alternatives, stakes, failure/partial outcomes and compatible endings. | References resolve; critical goals and endings are reachable; conditions cannot require facts that become available only after those conditions. |
| 3. Establish the shared world | Build a cast/world reference with stable actors, motives, knowledge, objects, locations, chronology, exact clue text and reveal predicates. | One identity per canonical fact; cross-scene clues agree; private truth and player-visible evidence are separate. |
| 4. Ground mechanics | Resolve monsters, quantities, hazards, rewards and encounters from the selected verified edition/source records and supported engine actions (EDG-019). | All required mechanics have executable supported references; the model cannot replace source statistics. Replan unsupported encounters privately. |
| 5. Build runnable scene packets | Specify entry conditions, observable material, NPC reactions, alternate approaches, exact discoveries and state changes for every relevant scene. | Essential progress has usable routes; an appropriate failed check yields a consequence or alternative rather than an accidental dead end. Exact clues and cross-scene promises retain their canonical identities. |
| 6. Write the manuscript | Expand scene packets into retained DM prose and approved opening passages (EDG-012). Pass each writer the necessary canonical records and connection requirements. | Prose agrees with packet facts and introduces no unregistered clue, reward, actor replacement or required mechanic. Parallelize only packets whose inputs are already stable. |
| 7. Review the complete campaign | Run deterministic structure/source checks plus a separate semantic critic pass over assembled artifacts (EDG-020). Record findings with evidence and affected IDs. | Chronology, motives, clues, branch consequences and endings agree across chapters; every critical finding is resolved. A writer's own success message is insufficient. |
| 8. Exercise alternate paths and repair | Traverse negotiation, combat, avoidance, failure, partial success and at least one unexpected plausible approach where relevant. Repair the affected plan/packets/prose and rerun dependent checks. | Supported paths reach compatible outcomes; amendments cannot rewrite revealed clues or irreversible facts. Automated fixtures establish consistency, not proof of human enjoyment. |
| 9. Seal the ready bundle | Freeze the exact manuscript/manifest/source/edition identities, validation report, internal reviewer/policy receipt and small player-safe opening seed. Publish atomically through the existing lifecycle. | All mandatory checks passed on the same artifact versions; readiness never depends solely on embedding success or a model saying it is done. |

This is a dependency workflow, not nine fixed model calls. A long campaign requires many
scene/act writing and review calls; mechanical reference checks and graph validation should
be code/tool operations. Scope controls the amount of preparation. Use bounded concurrency,
per-stage call/token budgets and repair limits. Retain completed artifacts so recovery reruns
only missing or invalidated work. Budget exhaustion or an unresolved critical finding yields
an honest non-spoiler preparation failure with a retry/recovery action, never silent readiness.

For example, if the critic finds the ferry keeper knows a clue before learning it, repair
the affected knowledge/reveal rules and dependent scenes, then revalidate their connections
and endings. Do not regenerate unrelated chapters or ask the player to approve that secret.

The semantic critic can use the configured runtime model in a separate call with the
assembled evidence and an explicit review task. Separate review context provides useful
scrutiny but does not guarantee independence or correctness; deterministic checks, retained
findings and path fixtures remain mandatory. Actual runtime provider/profile settings and
costs must be recorded when this pipeline is implemented and evaluated.

Player-facing states should describe only work, for example “Planning your adventure”,
“Preparing the world”, “Checking consistency”, and “Ready to play”. Do not display the number
of secret endings, private NPC names, actual branch labels or critique details. No player
approval of secret content is part of either source flow.

## How PDF preparation joins the same workflow

PDF extraction supplies the source-backed plan and exact quotations in place of inventing
them from a theme. The DM still performs scene/reference compilation, supported-mechanic
checks, whole-campaign review, path checks and internal publication. Required additions live
in a provenance-bearing DM extension layer; they do not rewrite original PDF spans or claim
to be printed book facts. The player sees the same preparation/readiness flow for both modes.

## Unexpected player actions

Prefer existing authored alternatives when they fit. For an unanticipated but plausible
approach, the planner proposes bounded consequences using current facts and engine rules.
Validate them before narration is treated as saved progress. If a new canonical definition
is needed, record a provenance-bearing amendment against the bound version; preserve all
already revealed quotations and irreversible outcomes. Reject contradictory amendments
with a useful alternative/recovery path. Full amendment publication/version policy needs
its own EDG-140 implementation and acceptance.

## Decisions offered for owner review

- **Recommended: meaningful authored branches plus validated amendments.** This supports
  agency without an impossible all-prompts tree. A stricter authored-only mode would be
  simpler but would constrain unexpected choices.
- **Owner-directed: autonomous private PDF preparation.** The DM resolves ambiguities,
  authors required connecting material and validates the result itself. Internal provenance
  distinguishes source facts from extensions. Player-facing content-review tasks are excluded.
- **Recommended: independent clock disclosures.** Current value, maximum and terminal state
  can each be revealed; numeric progress bars appear only when both required values are known.
- **Recommended: opening seeds committed with the opening narration.** This preserves one
  canonical transaction and idempotent reload/retry behavior.
- **Recommended: version-bound prepared bundles.** Private edits require internal DM
  validation and a new receipt; running saves retain their bound adventure identity.

## Implementation and acceptance boundary

The accompanying implementation establishes announcements, hint identity, place knowledge,
clock disclosure and explicit reviewed opening seeds in the shared manifest/story contract.
It integrates that contract into the journal and existing opening path. It leaves maps/art
(EDG-142) and broad accessibility verification (EDG-137) for the next batch, as requested.

Completing full EDG-140 still requires actual PDF and AI proposal producers, whole-backbone
validation, grounded encounters/rewards, amendment/version workflow and the full B-T01/B-T02
acceptance from its preserved brief. Demonstrating the consumer interface does not close
those requirements. Each producer must use the same contract, source identity and review
gates rather than adding a separate narrative database.
