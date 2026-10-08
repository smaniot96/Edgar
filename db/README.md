# db

Shared package, two subpackages:

`db.postgres` owns the SQLAlchemy 2.0 async engine, `async_session_factory`, the declarative `Base`, and the nine ORM models (`User`, `Campaign`, `Session`, `Character`, `CharacterAssignment`, `NPC`, `WorldFlag`, `CombatState`, `EventLog`). Apps import from `db.postgres` and `db.postgres.models`.

Integrity rules live in the database (revision `10_integrity`): deleting a campaign cascades to its sessions, NPCs, world flags and character assignments; deleting a session cascades to its event log and combat state; deleting a character cascades to its assignments and nulls `sessions.active_character_id` / `combat_state.active_character`. The ORM relationships use `passive_deletes=True` so they rely on those `ON DELETE` rules. `world_flags` is unique per `(campaign_id, key)` and `character_assignments` enforces `hp_current <= hp_max`. Every constraint and index is declared on the models too, so `alembic check` must report no drift (CI enforces it).

`db.vector` owns the Qdrant client factory, the OpenAI embedding helper (with retries), the canonical collection names, and the rules vs adventure retrieval helpers. See `db/vector/README.md`.

Alembic lives at `db/alembic/`. Run it via `make migrate` (the `migrate` Docker stage), or on the host from `db/`: `uv run --project .. python -m alembic upgrade head` (`make check-migrations` runs `alembic check`).
