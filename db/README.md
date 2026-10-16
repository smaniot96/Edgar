# db

Shared package, two subpackages:

`db.postgres` owns the SQLAlchemy 2.0 async engine, `async_session_factory`, the declarative `Base`, and the nine ORM models (`User`, `Campaign`, `Session`, `Character`, `CharacterAssignment`, `NPC`, `WorldFlag`, `CombatState`, `EventLog`). Apps import from `db.postgres` and `db.postgres.models`.

Integrity rules live in the database (revision `10_integrity`): deleting a campaign cascades to its sessions, NPCs, world flags and character assignments; deleting a session cascades to its event log and combat state; deleting a character cascades to its assignments and nulls `sessions.active_character_id`. The ORM relationships use `passive_deletes=True` so they rely on those `ON DELETE` rules. Revision `15_retire_combat_pointer` drops the unused `combat_state.active_character` pointer and refuses while any encounter still holds a value. `world_flags` is unique per `(campaign_id, key)` and `character_assignments` enforces `hp_current <= hp_max`. Every constraint and index is declared on the models too, so `alembic check` must report no drift (CI enforces it).

Revision `11_character_bounds` also enforces character levels from 1 to 20, positive maximum HP on characters and assignments, and nonnegative current assignment HP. It adds `(session_id, event_type, id)` to `event_log` for summary and narration-range lookups alongside the existing chronological index. Upgrade counts invalid historical rows and refuses them before changing the schema; correct those rows explicitly and retry. Upgrade and downgrade preserve all player/history data. The API's world-flag writer uses PostgreSQL `ON CONFLICT` so concurrent writes to one campaign/key do not race on uniqueness.

Populated-data downgrade outcomes: a downgrade that cannot restore what its upgrade moved or dropped refuses before changing anything, names the row count and says to restore from a pre-migration backup. A downgrade that only removes what its own upgrade added runs as written and discards values written since.

| Revision | Populated-data outcome |
| --- | --- |
| `a2e2bb9f88cc` (initial) | Refuses while `users` has rows: the base downgrade drops every table. |
| `1ad2ad6560f5` | Refuses while `characters` has rows: `session_id` cannot be rebuilt from `campaign_id`. Empty tables restore the column. |
| `2_add_event_log`, `3_campaign_adv`, `4_session_active_character`, `6_user_display_name`, `7_campaign_status` | Run as written. |
| `5_combat_state_extensions` | Refuses while any `combat_state.active_character` is NULL, because revision 4 requires it. |
| `8_character_assignments` | Refuses while `characters` has rows: library ownership, base copies and assignment history cannot return to revision 7. |

Revisions `9_combat_combatants` and `14_session_story_ledger` are outside this review. Their downgrades drop populated combat roster and story-fact data, so restore from backup before rolling back past them.

Sessions keep loaded fields available after commit (`expire_on_commit=False`); writers commit explicitly when their related changes are complete. Closing a session rolls back uncommitted work. The engine uses `pool_pre_ping` for stale idle connections and `hide_parameters` to keep private bound data out of SQLAlchemy errors; API logging separately removes PostgreSQL server row details. Application shutdown disposes the engine. Durable story events are separate from container diagnostics and must not be deleted using a log-rotation policy.

`db.vector` owns the Qdrant client factory, the OpenAI embedding helper (with retries), the canonical collection names, and the rules vs adventure retrieval helpers. See `db/vector/README.md`.

Alembic lives at `db/alembic/`. Run it via `make migrate` (the `migrate` Docker stage), or on the host from `db/`: `uv run --project .. python -m alembic upgrade head` (`make check-migrations` runs `alembic check`).
