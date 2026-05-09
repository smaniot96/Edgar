# Edgar
Edgar is your companion for your dnd session. Enjoy!

## Docker Compose

**Start without rebuilding images** (keeps existing `edgar-*` images; does not run `docker build`):

```bash
docker compose up -d --no-build
```

**Only API + dependencies** (skip `agent`, `ingestion`, `rules_engine` if you don’t need those containers):

```bash
docker compose up -d --no-build db redis qdrant api
```

- **Qdrant** and **Postgres** use **named volumes** (`qdrant_data`, `postgres_data`) so collections and DB data survive container restarts. They are **not** rebuilt from a Dockerfile—only the pulled image is used.
- Use `docker compose build <service>` only when you change that service’s Dockerfile or app code.

**First time after adding the Qdrant volume:** if you already had collections in an old Qdrant container without this volume, back them up or re-run ingestion after the new volume mounts (empty volume replaces in-container storage on first create).
