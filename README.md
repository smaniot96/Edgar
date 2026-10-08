# Edgar

A solo Dungeon Master AI that runs a D&D 5e campaign and lets you play it from a chat UI in your browser. PostgreSQL holds all truth; an LLM proposes outcomes, the rules engine and dice tool decide them, and the database persists what happened.

## Stack

Python 3.12 with `uv` workspaces. FastAPI for the HTTP layer; Pydantic v2 schemas mirror SQLAlchemy 2.0 async ORM models. Postgres 16 stores users, campaigns, sessions, characters, NPCs, world flags, combat state, and an append-only event log; Alembic owns migrations. Qdrant stores embedded chunks of the rulebooks (PHB, DMG, MM) and the adventure module (Chalice of the Mountain God by default), with `text-embedding-3-small`. The agent is a LangGraph state machine (input parser, world retriever, rules adjudicator, world state updater, narrator, memory summarizer) with a combat subgraph that is invoked when intent classifies as combat. Redis carries a per-session turn lock so two requests cannot race a turn.

The chat UI is a React SPA under [`frontend/`](frontend/README.md), served by the API at `/ui` after `npm run build` (included in the Docker API image).

## Quick start

```
cp .env.example .env          # set OPENAI_API_KEY
make up                        # docker compose up + migrate + seed
open http://localhost:8000/ui
```

For local UI development with hot reload, run the API and Vite in two terminals (`make frontend-dev` uses port **5173** and proxies `/api` to the API):

```
make frontend-install          # once
# Terminal A: start API (e.g. docker compose up api, or uvicorn from repo root)
# Terminal B:
make frontend-dev
open http://localhost:5173/ui/
```

`make up` runs Alembic (`make migrate`), brings the stack up, polls `/health` until the API answers (default 60s; override with `WAIT_TIMEOUT_SEC`), then hits `/api/seed`. Use `make logs` to tail the API container, `make down` to stop. `/health` is liveness only; `/ready` also checks Postgres, Redis and Qdrant (503 with a per-component breakdown if one is down).

### Compose services and profiles

`docker compose up -d` starts only the runtime stack: `api`, `db`, `redis`, `qdrant`. One-off jobs sit behind profiles and are run on demand (`docker compose run` enables a service's profile automatically):

| Service | Profile | How to run |
| --- | --- | --- |
| `migrate` | `tools` | `make migrate` (also part of `make up`) |
| `ingestion` | `tools` | `make ingest PDF=... COLLECTION=...` |
| `rules_engine` | `rules` | `make rules` (rules-only RAG demo) |

Postgres (5432), Redis (6379) and Qdrant (6333) are published on `127.0.0.1` only; the API listens on port 8000. Redis and Qdrant images are pinned (`redis:8.6.3`, `qdrant/qdrant:v1.16.3`); bump them deliberately. Images run as a non-root user and each Dockerfile target installs only its own workspace package (`uv sync --package ...`); the `api` image includes `ingestion` because uploads and AI-authored campaigns are embedded in-process. `docker-compose.override.yml` (gitignored, dev only) bind-mounts the source and runs uvicorn with `--reload`.

Uploads and AI generation run as in-process background tasks. If the API restarts mid-way, the adventure is marked `failed` on the next startup; `POST /api/adventures/{slug}/retry` re-runs it.

## Playing

Drop your PDFs into `data/pdfs/` (gitignored) and ingest them, then send turns from the UI:

```
make ingest PDF=player_handbook.pdf COLLECTION=rules_player_handbook
make ingest PDF=dm_guide.pdf        COLLECTION=rules_dm_guide
make ingest PDF=monster_manual.pdf  COLLECTION=rules_monster_manual
make ingest PDF=chalice_of_the_mountain_god.pdf COLLECTION=chalice_of_the_mountain_god
```

`make ingest` runs the ingestion container against the bind-mounted `data/pdfs/`. See `apps/ingestion/README.md` for collection naming, campaign-bound notes (`--campaign-id`), and the underlying CLI flags.

You can also play without a UI:

```
curl -N -X POST http://localhost:8000/api/sessions/1/turn/stream \
  -H 'content-type: application/json' \
  -d '{"message":"I look around the tavern"}'
```

The `/turn/stream` endpoint emits Server-Sent Events (`status`, `token`, `adjudication`, `done`, `error`); `/turn` returns one JSON. `apps/api/README.md` documents both.

## Repo layout

```
Edgar/
  frontend/          React + Vite + Tailwind SPA (npm); served at /ui by the API
  apps/
    api/             FastAPI: routers, schemas, services; bundles frontend/dist in Docker
    agent/           LangGraph DM agent (graph, nodes, models, prompts)
    ingestion/       PDF -> chunks -> embeddings -> Qdrant
    rules_engine/    Rules-only RAG demo (CLI)
  core/              edgar_core: config and env loading
  db/
    postgres/        SQLAlchemy 2.0 async models, session, base
    alembic/         Migrations
    vector/          Qdrant client, embeddings, rules vs adventure retrieval
  tools/             Dice tool (deterministic, used by the agent)
  data/              PDFs and extracted markdown (gitignored, bind-mounted)
  tests/             Pytest suite (testcontainers Postgres, fakeredis, fake LLM)
  Dockerfile         Multi-stage: python-base / migrate / api / ingestion / rules_engine
  docker-compose.yml api + db + redis + qdrant; migrate/ingestion/rules_engine behind profiles
  pyproject.toml     uv workspace root
  Makefile           up / down / logs / migrate / seed / ingest / rules / test / lint / check-migrations / frontend-*
```

`db/`, `tools/`, `core/`, and the four apps are independent uv workspace members; the workspace root lockfile is the only `uv.lock` you should regenerate.

## Configuration

`config.py` lives in `core/src/edgar_core/config.py`. It reads component env vars (`DB_HOST`, `POSTGRES_USER`, etc.) and builds the URLs the rest of the code consumes (`DATABASE_URL`, `REDIS_URL`, `VECTOR_DB_URL`). Docker overrides only host names so the same `.env` works on the host (via `localhost`) and inside Compose (via `db`, `redis`, `qdrant`). `LLM_MODEL` and `EMBEDDING_MODEL` are env-overridable; defaults are `gpt-4o-mini` and `text-embedding-3-small`.

## Tests

```
make test
```

The suite spins up Postgres 16 via testcontainers (running `alembic upgrade head`), fakes Redis with `fakeredis`, fakes the OpenAI LLM with a deterministic stub, and runs the full turn flow end to end. Docker must be reachable: locally the DB-backed tests are skipped without it, but with `CI` set they fail instead. Tests never use a real OpenAI key (`OPENAI_API_KEY` defaults to a dummy value).

After changing a model, add an Alembic migration in `db/alembic/versions/` and confirm `make check-migrations` (`alembic check` against your migrated database) reports no drift.

## CI

`.github/workflows/ci.yml` runs on pushes and PRs to `main`:

* **edgar-ruff**: `uv sync --locked --group dev`, then `ruff check .` (rules `E,F,I,B,UP`; `claude-report/`, `audit/`, `plans/` are excluded). Formatting is not enforced yet.
* **edgar-tests**: the pytest suite with `CI=true` (missing Docker fails the job).
* **edgar-migrations**: against a Postgres service container, `alembic upgrade head`, `alembic check` (models vs migrations drift), and a downgrade/upgrade of the latest revision.
* **docker-build**: builds the `api` and `migrate` Docker targets.
* **frontend**: `npm ci`, `npm run lint`, `npm test --if-present`, `npm run build`.

CI pins uv to the version that writes `uv.lock`; regenerate the lock with that version (`uv lock`).
