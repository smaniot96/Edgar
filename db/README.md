# db

Shared package, two subpackages:

`db.postgres` owns the SQLAlchemy 2.0 async engine, `async_session_factory`, the declarative `Base`, and the nine ORM models (`User`, `Campaign`, `Session`, `Character`, `CharacterAssignment`, `NPC`, `WorldFlag`, `CombatState`, `EventLog`). Apps import from `db.postgres` and `db.postgres.models`.

Integrity rules live in the database (revision `10_integrity`): deleting a campaign cascades to its sessions, NPCs, world flags and character assignments; deleting a session cascades to its event log and combat state; deleting a character cascades to its assignments and nulls `sessions.active_character_id` / `combat_state.active_character`. The ORM relationships use `passive_deletes=True` so they rely on those `ON DELETE` rules. `world_flags` is unique per `(campaign_id, key)` and `character_assignments` enforces `hp_current <= hp_max`. Every constraint and index is declared on the models too, so `alembic check` must report no drift (CI enforces it).

Revision `11_character_bounds` also enforces character levels from 1 to 20, positive maximum HP on characters and assignments, and nonnegative current assignment HP. It adds `(session_id, event_type, id)` to `event_log` for summary and narration-range lookups alongside the existing chronological index. Upgrade counts invalid historical rows and refuses them before changing the schema; correct those rows explicitly and retry. Upgrade and downgrade preserve all player/history data. The API's world-flag writer uses PostgreSQL `ON CONFLICT` so concurrent writes to one campaign/key do not race on uniqueness.

Sessions keep loaded fields available after commit (`expire_on_commit=False`); writers commit explicitly when their related changes are complete. Closing a session rolls back uncommitted work. The engine uses `pool_pre_ping` for stale idle connections and `hide_parameters` to keep private bound data out of SQLAlchemy errors; API logging separately removes PostgreSQL server row details. Application shutdown disposes the engine. Durable story events are separate from container diagnostics and must not be deleted using a log-rotation policy.

`db.vector` owns the Qdrant client factory, the OpenAI embedding helper (with retries), the canonical collection names, and the rules vs adventure retrieval helpers. See `db/vector/README.md`.

Alembic lives at `db/alembic/`. Run it via `make migrate` (the `migrate` Docker stage), or on the host from `db/`: `uv run --project .. python -m alembic upgrade head` (`make check-migrations` runs `alembic check`).
