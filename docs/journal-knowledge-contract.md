# Journal knowledge and reviewed opening contract

Draft implemented for owner review, 15 October 2026. Consumers: EDG-033/041;
producer integration: EDG-140. EDG-142 and EDG-137 are outside this batch.

Prepared definitions remain private in the existing version-bound, explicitly reviewed
manifest. Session facts remain in the existing atomic story ledger. Reading a journal
never creates knowledge, loads a private plan, or calls a model.

| Concept | Prepared representation | Committed player knowledge |
| --- | --- | --- |
| Announced goal/milestone | `announcement_passage_ids` on its definition; `announced` allowed in `statuses` | An `objective`/`milestone` fact with `status=announced`, exact reviewed announcement text. It is not an active objective. |
| Earned hint | Entity `kind=hint`, normal reviewed passage prerequisites/reveal rules | A validated `discover` fact stamped `knowledge_kind=hint`; unknown hints never enter list/search/export. |
| Known place | Player passage `known_scene_ids` naming canonical scenes | `learn_place` with `status=known`. A separate `visit`/`move` earns `visited`; only those can orient current location. Mentioning a place does not authorize movement. |
| Clock disclosure | `clock_reveal.current_requires`, `maximum_requires`, `completion_requires` | Validator-stamped `clock_disclosure`. Each field is independently gated by committed session predicates. `null` withholds; `{}` allows after earning the clock; a predicate map requires exact matches. |
| Reviewed opening | Explicit `opening_journal` seed entries, referring only to unconditional player passages in the opening scene | Exact verified passage text sealed and persisted with the first narration in its caller-owned transaction. Retry cannot duplicate facts. |
| Readable item ownership | Optional clue/hint `object_id` binds a reviewed object definition | Validator-stamped `related_object` allows matching the session's committed acquire/transfer/consume facts without conflating learned text and ownership. |

Every disclosure is part of the review digest and immutable effect identity. Absent
extensions preserve legacy manifest receipts and effect IDs. Adding/changing an extension
requires explicit review; there is no automatic re-approval, live migration or prose backfill.

Clock facts without explicit disclosure remain qualitative: no value, maximum, inferred
completion, hidden history count or hidden-update ordering is serialized by the journal.
A revealed current value alone never implies that its private maximum is known.

Opening seeds support discovered clues/hints, encountered actors, the visited opening,
explicitly known other places, and earned/announced goals or milestones. They are declared
by the private DM preparation workflow and validated against exact source evidence. The
player never reviews private adventure content. Generation alone does not establish a
valid review receipt. Other prepared branches
remain private. Unsupported/ambiguous seeds fail validation rather than inventing facts.

This consumer contract does not itself complete EDG-140's full PDF/AI backbone generation,
branch validation, manuscript grounding and publication acceptance. Those requirements
remain in its full brief. Integration must retain source/version identity and existing
explicit review/publication gates.
