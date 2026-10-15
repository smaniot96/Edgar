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

Upload PDF → private DM preparation → ready to play. The player sees preparation progress,
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

## AI workflow proposal

1. Bind the actual hero/party, selected edition and verified source books (EDG-019).
2. Draft the private quest/scene/branch structure before expanding chapters. Resolve exact
   creatures, quantities, encounters, rewards and hazards using supported engine/source records.
3. Write the retained manuscript from that structure (EDG-012), preserving stable identities
   and exact clue text. Significant structure changes become explicit proposed amendments.
4. Check manuscript/structure agreement, source grounding and branch reachability (EDG-020).
   Unverified mechanics or unresolved critical references prevent verified publication.
5. The DM runs its internal review and publishes the validated version-bound bundle. Produce
   a small opening seed from the reviewed player passages; the browser never receives the
   private plan or a content-review task.

Recommendation: internal draft → validating/repairing → ready states. The player sees
preparation progress and readiness. A successful generation call alone is insufficient;
publication requires the recorded validation receipt. No player approval of secret content
is part of the flow.

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
