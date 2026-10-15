# Session story state contract

`load_story_state(db, session_id, campaign_id=None)` returns the exact `StoryState` JSON
shape from `edgar_core.story`. `facts` is an ordered append-only history of accepted effects;
consumers select the latest effect per `(kind, fact_id)` for current progress. `fact_id` is
an opaque stable hash of a source/version-qualified canonical target. `effect_id` hashes
accepted effect content and uniquely identifies an immutable effect within a session.

`persist_story_effects(db, game_session, effects, *, event_id, expected_state=None)` runs
inside the caller's existing transaction. The narration EventLog must already be flushed.
The helper locks the session, verifies event/session/campaign ownership and effect identity,
compares the full expected committed story snapshot when supplied, rejects immutable clue
changes and terminal regressions, and stages the effects and compatible encountered NPCs.
It never commits or calls a model. The caller must roll back the transaction on any conflict.
Scene, inventory and engine-number revalidation remain the turn persistence caller's job.

Session-derived NPCs have a nullable `session_id` and opaque `story_identity`; existing
campaign-wide manual NPC rows retain null values and their existing data. Session NPC
loaders include manual rows and only that session's derived rows. The NPC listing accepts
`?session_id=`; without it only legacy manual rows are returned. Direct reads of a derived
NPC require its matching session ID. Derived NPC mutation is through accepted story effects.

`session_story_flags(state, manifests, legacy_flags)` removes prepared discovery-flag keys
from campaign-wide flags, then derives `discovered` values from this session's earned facts
whose source and manifest versions match. This preserves manual flag behavior without
sharing new discoveries between playthroughs. New discovery effects must never be persisted
as campaign-wide WorldFlags.

`player_known_story(state)` is the server-side EDG-041 journal projection. It accepts only
committed state, never prepared manifests. Its fixed `facts` allowlist exposes opaque IDs,
kind/status, earned text, a name only when literally present in earned text, progress quantity,
public source page/hash origins and event provenance. It omits canonical target IDs, gates,
private metadata and object bookkeeping. Facts lacking valid matching player-audience source
origins are excluded, including engine-only outcomes without an explicit public source.
This projection has no unknown/future fact counts or prepared definitions. Narration uses the same core projection, including source validation and earned names.
Player-authored promises retain the exact utterance, encountered actor anchor and original
server turn; evidence_kind distinguishes speech from a source quotation.

Resolved engine combat outcomes use the existing session-owned `CombatState` rows.
`load_resolved_combat_outcomes(db, session_id)` restores all ended encounters in stable row-ID
order with their outcome, round and actual persisted roster identities. Initial agent state
loads these as `resolved_combat_outcomes`, independently of the current active encounter.
The planner treats these records as authoritative prior results. `resolved_combat_outcomes_public`
projects the same records to known results/encountered roster names without private creature
source bindings. The journal should join this projection with `player_known_story` using the
existing combat row ID as identity; it must not fabricate StoryRefs or duplicate combat
outcomes as new ledger rows. Outcomes are irreversible engine state, not model proposals.
