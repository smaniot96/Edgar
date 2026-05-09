# db

Shared package, two subpackages:

`db.postgres` owns the SQLAlchemy 2.0 async engine, `async_session_factory`, the declarative `Base`, and the eight ORM models (`User`, `Campaign`, `Session`, `Character`, `NPC`, `WorldFlag`, `CombatState`, `EventLog`). Apps import from `db.postgres` and `db.postgres.models`.

`db.vector` owns the Qdrant client factory, the OpenAI embedding helper (with retries), the canonical collection names, and the rules vs adventure retrieval helpers. See `db/vector/README.md`.

Alembic lives at `db/alembic/`. Run from the Edgar root via the workspace alembic.ini, or via `make migrate` (which runs the `migrate` Docker stage).
