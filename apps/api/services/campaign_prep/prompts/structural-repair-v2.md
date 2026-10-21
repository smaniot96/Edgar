Repair only the supplied typed structural defects. This is join recovery, not
campaign reauthoring. Return Bundles containing exactly the supplied affected
chapters in their original order. Preserve all row positions and every field
exactly except the keys named in allowed_fields at the supplied chapter, field,
and zero-based row index. Preserve source prose, stat blocks, quantities,
procedures, origins, motives, secrets and all unaffected records verbatim.

For an identity collision, preserve the canonical entity/secret identity and
give the colliding event or goal a new unique stable ID. Update each actual
typed consumer of that event/goal ID (including goals, next events, item and
check event_ids). Never rename the NPC merely because its event shares a name.
Resolve dangling next_event_ids to existing intended events or remove an
unsupported edge. Do not invent extra scenes.

An encounter actor must identify a canonical NPC and occur in that event's
participants. Correct a mistaken actor reference to an existing matching NPC,
or add only a specifically permitted allowed_new_actors NPC if the source
requires that missing identity. Keep encounter quantities and mechanics exactly.
Never add a remote NPC as physically present just to satisfy a reference.

A typed reveal's entity must be an event participant or clue. If the discovery
describes a remote NPC/place, preserve that information in discovery guidance
appended to the existing dm_notes and remove the invalid typed reveal. The
existing dm_notes must remain an exact prefix. Do not lose the reveal's
public_text or condition; retain both in the appended guidance. Do not put a
remote NPC into participants or disguise an NPC/place as a clue.

Use the complete relevant original OCR and page images to disambiguate identity
and physical presence. These are authoritative evidence. Do not summarize or
rewrite accepted chapter source_notes during structural recovery.
