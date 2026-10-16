# db

Shared package, two subpackages:

`db.postgres` owns the SQLAlchemy 2.0 async engine, `async_session_factory`, the declarative `Base`, and the nine ORM models (`User`, `Campaign`, `Session`, `Character`, `CharacterAssignment`, `NPC`, `WorldFlag`, `CombatState`, `EventLog`). Apps import from `db.postgres` and `db.postgres.models`.

Integrity rules live in the database (revision `10_integrity`): deleting a campaign cascades to its sessions, NPCs, world flags and character assignments; deleting a session cascades to its event log and combat state; deleting a character cascades to its assignments and nulls `sessions.active_character_id`. The ORM relationships use `passive_deletes=True` so they rely on those `ON DELETE` rules. Revision `15_retire_combat_pointer` drops the unused `combat_state.active_character` pointer and refuses while any encounter still holds a value. `world_flags` is unique per `(campaign_id, key)` and `character_assignments` enforces `hp_current <= hp_max`. Every constraint and index is declared on the models too, so `alembic check` must report no drift (CI enforces it).

Revision `11_character_bounds` also enforces character levels from 1 to 20, positive maximum HP on characters and assignments, and nonnegative current assignment HP. It adds `(session_id, event_type, id)` to `event_log` for summary and narration-range lookups alongside the existing chronological index. Upgrade counts invalid historical rows and refuses them before changing the schema; correct those rows explicitly and retry. Upgrade and downgrade preserve all player/history data. The API's world-flag writer uses PostgreSQL `ON CONFLICT` so concurrent writes to one campaign/key do not race on uniqueness.

Sessions keep loaded fields available after commit (`expire_on_commit=False`); writers commit explicitly when their related changes are complete. Closing a session rolls back uncommitted work. The engine uses `pool_pre_ping` for stale idle connections and `hide_parameters` to keep private bound data out of SQLAlchemy errors; API logging separately removes PostgreSQL server row details. Application shutdown disposes the engine. Durable story events are separate from container diagnostics and must not be deleted using a log-rotation policy.

## Event retention

`event_log` holds replayable story history and developer telemetry in one table. `EVENT_RETENTION` in `db/postgres/models/event_logs.py` classifies every `event_type` that production code writes; `tests/db/test_event_retention.py` fails when a written type has no class, so a new type cannot become expirable or durable by accident. Current inventory:

| event_type | Written by | Read by | Class |
| --- | --- | --- | --- |
| `narration` | `turn_runner.persist_turn`; opening scene in `session_intro` | chat reload (`GET /sessions/{id}/messages`); journal and export (`services/journal.py`); NPC tracking (`routers/npc.py`); turn continuity and summary coverage (`services/session_messages.py`); idempotent turn receipts (`turn_receipts.py`, `turn_runner.py`); story-fact ownership (`story_state.py`); `story_facts.event_id` cascades from it | durable |
| `adjudication` | `world_state_updater_node`, written by `persist_turn` | private ruling record with exact references (`tests/test_source_contract.py`); no application reader yet | durable |
| `memory_summary` | `session_messages._publish_memory_summary` | `load_memory_summary` and the summary chain, which feed the narrator's story-so-far | durable |
| `combat_round` | `combat_round_event`, staged by `combat_round_node` | developer diagnostics `GET /sessions/{id}/diagnostics/combat` (`EDGAR_DEBUG` only) | diagnostic |

Policy:

- Durable rows never expire. Deleting a session (`DELETE /sessions/{id}`) cascades to its events; that is an explicit user action, not a retention rule.
- Diagnostic rows may expire after a window. No expiry code exists: at the measured 126 rows none is needed. Any future expiry needs an explicit operator entry point.
- Before any durable row is deleted or pruned, it must be archived or exported to storage independent of the database. The journal export is a player-safe projection, not a raw archive. No verified backup or restore drill exists yet, so deletion code waits for one.
- Container-log rotation (Compose `json-file`, three 10 MB files) does not apply to database story data.
- A new `event_type` is classified in `EVENT_RETENTION` in the same change that writes it.

`db.vector` owns the Qdrant client factory, the OpenAI embedding helper (with retries), the canonical collection names, and the rules vs adventure retrieval helpers. See `db/vector/README.md`.

Alembic lives at `db/alembic/`. Run it via `make migrate` (the `migrate` Docker stage), or on the host from `db/`: `uv run --project .. python -m alembic upgrade head` (`make check-migrations` runs `alembic check`).
