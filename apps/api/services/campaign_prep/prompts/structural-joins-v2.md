The prepared adventure may coherently adapt the PDF. Source differences alone
do not require correction; preserve the accepted plot and facts while fixing joins.

Repair only the supplied typed joins. Return StructuralJoins: sparse row changes
and specifically permitted missing NPC records. Do not output chapter bundles,
source notes, encounter profiles, item procedures or unchanged narrative prose.

Address rows using the supplied chapter_id, collection field, zero-based index
and original expected_id (entity_id for item rows). Emit only changed permitted
keys. Scalar changes contain id/location_id; list changes contain the complete
replacement ID list for that permitted key. Do not return an unchanged row.
Every allowed_fields entry is a permission, not a demand to change that field.

For identity collisions preserve the canonical entity/secret. Rename the
colliding event or goal with a unique ID and update all actual typed consumers,
including item/check event_ids. Resolve dangling next_event_ids to intended
existing events or remove unsupported edges. Do not invent extra scenes.

An encounter actor must be a canonical NPC and occur in the event participants.
Use actors entries to change only actor_id at the original encounter index.
Correct an identity to an existing matching NPC, or add only an NPC named in
allowed_new_actors under new_actors when the source requires that missing actor.
Retain source identity, motive and origin for a new actor. Do not add a remote
NPC as physically present merely to satisfy a reference. Quantities, profiles
and all other encounter content are retained locally and cannot be changed.

For each invalid reveal, identify its original zero-based reveal index. Set
entity_id to a local participant/clue only when that is the correct discovery
subject. Set entity_id to null for a remote discovery: its exact original text
and condition will be retained locally as private DM guidance. Never retype or
paraphrase that text. Already-valid reveals cannot be changed.

Use accepted chapter content and compatible original OCR/images as evidence;
respect coherent adaptations. Some joins may already have been recovered from a retained paid
response; repair only the residual defects shown here. Return no other edits.
