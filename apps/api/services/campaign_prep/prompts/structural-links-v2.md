Repair the complete supplied residual reference audit in one sparse response.
Return StructuralLinks containing only permitted typed row joins, item identity
joins and specifically permitted new canonical entities. Never output chapters,
source notes, source procedures, stat blocks or rewritten narrative content.

Address rows using chapter_id, field, original zero-based index and expected_id.
For item identity joins, expected_id is the original item.entity_id. Retarget the
item row to an existing matching clue/object entity, or create the clue entity
under the supplied permitted_object_id and reference it. An item is a separate
object from the NPC who carries it or the corpse where it lies. Preserve every
NPC identity and kind. Never turn a creature, person or place into a clue to
satisfy an item reference. Item procedures and source origins are retained locally.

Use new_entities only for exact identities listed in allowed_new_entities and
only a permitted kind. Original OCR, images, accepted procedures and chapter
running notes establish what the entity is; do not invent a missing source fact.
Correct a mistaken reference to a matching existing canonical entity whenever
appropriate, especially spelling variants. Preserve actual entity identity across
all its consumers. Do not introduce duplicate people, places or objects.

For rows, changes contain only the explicitly permitted scalar id/location_id
or complete replacement ID lists. Resolve all listed dangling or wrongly typed
references together. Source item/check event_ids must reference existing events
in their own chapter. Goals must retain at least one real event. Existing actors
must remain in their encounter's participants; remote entities must not become
physically present merely to satisfy an unrelated reference.

Return no unchanged rows and no other edits. All accepted prose, encounter
mechanics, origins, discovery wording, checks and item procedures are immutable.
Human-readable section and area labels are already preserved in origin.note;
origin.regions may contain only actual OCR region IDs from the cited pages.
