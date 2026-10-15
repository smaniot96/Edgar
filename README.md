# Edgar

A solo Dungeon Master AI that runs a D&D 5e campaign and lets you play it from a chat UI in your browser. PostgreSQL holds all truth; an LLM proposes outcomes, the rules engine and dice tool decide them, and the database persists what happened.

## Stack

Python 3.12 with `uv` workspaces. FastAPI for the HTTP layer; Pydantic v2 schemas mirror SQLAlchemy 2.0 async ORM models. Postgres 16 stores users, campaigns, sessions, characters, NPCs, world flags, combat state, and an append-only event log; Alembic owns migrations. Qdrant stores embedded chunks of the rulebooks (PHB, DMG, MM) and the adventure module (Chalice of the Mountain God by default), with `text-embedding-3-small`. The agent is a LangGraph state machine (input parser, world retriever, rules adjudicator, world state updater, narrator, memory summarizer) with a combat subgraph that is invoked when intent classifies as combat. Redis carries a per-session turn lock so two requests cannot race a turn.

The chat UI is a React SPA under [`frontend/`](frontend/README.md), served by the API at `/ui` after `npm run build` (included in the Docker API image).

## Quick start

```
cp .env.example .env          # set OPENAI_API_KEY
make build                   # build API (including UI) and migration images
make up                      # migrate, start the runtime stack, wait, seed
open http://localhost:8000/ui
```

For local UI development with hot reload, run the API and Vite in two terminals (`make frontend-dev` uses port **5173** and proxies `/api` to the API):

```
make frontend-install          # once
# Terminal A (after make build):
make dev-up                    # current backend source + hot reload
# Terminal B:
make frontend-dev
open http://localhost:5173/ui/
```

`make up` runs Alembic (`make migrate`), brings the stack up, polls `/health` until the API answers (default 60s; override with `WAIT_TIMEOUT_SEC`), then hits `/api/seed`. Use `make logs` to tail the API container, `make down` to stop. `/health` is liveness only; `/ready` also checks Postgres, Redis and Qdrant (503 with a per-component breakdown if one is down).

Run `make build` again after changing dependencies or migrations, or to update the baked UI/backend in base mode. Development mode mounts current backend and migration source; dependency changes still need a rebuild. The API at port 8000 serves the UI built into its image; use Vite on port 5173 for frontend hot reload. When switching between base and development modes, run the corresponding `make up` or `make dev-up` to recreate services with that configuration.

### Compose services and profiles

`docker compose up -d` starts only the runtime stack: `api`, `db`, `redis`, `qdrant`. One-off jobs sit behind profiles and are run on demand (`docker compose run` enables a service's profile automatically):

| Service | Profile | How to run |
| --- | --- | --- |
| `migrate` | `tools` | `make migrate` (also part of `make up`) |
| `ingestion` | `tools` | `make ingest PDF=... COLLECTION=...` |
| `rules_engine` | `rules` | `make rules` (rules-only RAG demo) |

All services are published on `127.0.0.1` only, including the API on port 8000. Redis and Qdrant images are pinned (`redis:8.6.3`, `qdrant/qdrant:v1.16.3`); bump them deliberately. Images run as a non-root user and each Dockerfile target installs only its own workspace package (`uv sync --package ...`); the `api` image includes `ingestion` because uploads and AI-authored campaigns are embedded in-process.

Compose explicitly rotates container output with the `json-file` driver, retaining up to three 10 MB files per container. `make logs` reads this output; recreating a service applies changed logging options. PostgreSQL's `event_log` stores campaign history and adjudications as application data. Container log rotation does not delete that history.

`docker-compose.dev.yml` is a tracked, optional overlay: it mounts backend source, runs uvicorn with `--reload`, enables developer diagnostics, and mounts current migrations into the migration job. Select it with `make dev-up` or `docker compose -f docker-compose.yml -f docker-compose.dev.yml ...`. Matching `dev-build`, `dev-migrate`, `dev-logs` and `dev-down` targets use the same files. Plain Make targets select only `docker-compose.yml` and do not auto-load personal overrides. The former local `docker-compose.override.yml` was removed; Compose no longer automatically applies it.

Uploads and AI generation run as in-process background tasks. If the API restarts mid-way, the adventure is marked `failed` on the next startup; `POST /api/adventures/{slug}/retry` re-runs it.

## Playing

Drop your PDFs into `data/pdfs/` (gitignored). Extract and inspect each complete rulebook
before indexing it, then send turns from the UI:

```
make ingest PDF=player_handbook.pdf INGEST_ARGS='--extract-only'
# Inspect data/markdown/player_handbook.md and its extraction JSON against the PDF.
make ingest PDF=player_handbook.pdf COLLECTION=rules_player_handbook INGEST_ARGS='--reviewed-extraction /output/markdown/player_handbook.extraction.json'
make ingest PDF=chalice_of_the_mountain_god.pdf COLLECTION=chalice_of_the_mountain_god
```

Repeat the extract/review steps for `dm_guide.pdf` and `monster_manual.pdf`, using their
matching `rules_*` collections and extraction bundles. `make ingest` runs the ingestion
container against the bind-mounted `data/pdfs/`; `INGEST_ARGS` passes CLI options through.
Rulebook indexing and PDF replacement (`--replace`) require a reviewed full-document
bundle. See `apps/ingestion/README.md` for diagnostics, recovery, collection naming and
campaign-bound notes (`--campaign-id`).

You can also play without a UI:

```
curl -N -X POST http://localhost:8000/api/sessions/1/turn/stream \
  -H 'content-type: application/json' \
  -d '{"message":"I look around the tavern"}'
```

The `/turn/stream` endpoint emits Server-Sent Events (`status`, `token`, `adjudication`, `done`, `error`); `/turn` returns one JSON. `apps/api/README.md` documents both.

### Rules scope

The engine simulates a deliberately simplified subset of 5e, and declares what it does not
simulate (`GET /api/rules/supported-scope`):

- Executed by the engine: d20 ability, skill, save and tool checks against DCs of 10/15/20/25;
  weapon attacks with natural 1 and 20 handling; AC from the armor table; initiative; combat rounds
  in initiative order; Fighter Second Wind (one use, spent by the player's action in or out of
  combat and restored by a short or long rest; no automatic use at 0 HP); short and long rest
  healing; healing potions taken from inventory.
- Not supported: advantage and disadvantage, grapple/shove/pin/restraint, declared spell effects,
  spell slots and concentration, condition effects, cover and positioning, bonus actions and
  reactions, the Hit Dice ledger, and class features beyond Second Wind. A declared action of this
  kind gets a narrative ruling and no mechanical effect.
- Solo adaptations: defeat leaves the hero subdued at 1 HP rather than dead, and encounters are
  scaled down for a single level-1 hero.

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
  work-register/     Optional local-only register, individual work briefs and audit evidence
  tests/             Pytest suite (testcontainers Postgres, fakeredis, fake LLM)
  Dockerfile         Multi-stage: python-base / migrate / api / ingestion / rules_engine
  docker-compose.yml api + db + redis + qdrant; migrate/ingestion/rules_engine behind profiles
  docker-compose.dev.yml  Opt-in backend hot reload and current migration source
  pyproject.toml     uv workspace root
  Makefile           build / up / down / logs / migrate / dev-* / seed / ingest / rules / test / lint / check-migrations / frontend-*
```

`db/`, `tools/`, `core/`, and the four apps are independent uv workspace members; the workspace root lockfile is the only `uv.lock` you should regenerate.

The optional personal [work register](work-register/REGISTER.md) is the local planning entry point: choose an Open item, follow its linked Markdown brief, then ask the implementing agent to start by ID. Each brief includes scope, acceptance criteria, dependencies and preserved audit evidence. The register, story library and copied reports/playtest evidence are gitignored, excluded from Docker build contexts and absent from a fresh clone. Repository setup and operation do not require them. The superseded root `audit/` folder was removed after the independent coverage review passed; the library retains its evidence independently. The intentionally deleted old database dump and ingestion logs were not recreated.

## Configuration

`config.py` lives in `core/src/edgar_core/config.py`. It reads component env vars (`DB_HOST`, `POSTGRES_USER`, etc.) and builds the URLs the rest of the code consumes (`DATABASE_URL`, `REDIS_URL`, `VECTOR_DB_URL`). Docker overrides only host names so the same `.env` works on the host (via `localhost`) and inside Compose (via `db`, `redis`, `qdrant`). `LLM_MODEL` and `EMBEDDING_MODEL` are env-overridable; defaults are `gpt-6-luna` and `text-embedding-3-small`. Reasoning models (`gpt-5*`, `gpt-6*`, `o*`) only accept the default temperature, so the agent sends `LLM_REASONING_EFFORT` (default `low`) instead and calls them through the OpenAI Responses API (Chat Completions rejects function-calling structured output combined with a reasoning effort on these models); classic models such as `gpt-4o-mini` still get per-node temperatures.

The API sends structlog, standard-library/server logs and in-process ingestion logs through one output formatter. `ENV=local` uses readable console output; other values use JSON with UTC timestamps, log level, logger name and request correlation. `X-Request-ID` accepts bounded IDs from a proxy and is echoed on responses, including unhandled errors. Common credential fields and credential URL forms are redacted, SQL bound parameters are hidden, and traceback locals are disabled. Avoid logging request bodies, prompts or arbitrary secrets: pattern-based redaction is not a complete privacy policy.

## Tests

```
make test
```

The suite spins up Postgres 16 via testcontainers (running `alembic upgrade head`), fakes Redis with `fakeredis`, fakes the OpenAI LLM with a deterministic stub, and runs the full turn flow end to end. Docker must be reachable: locally the DB-backed tests are skipped without it, but with `CI` set they fail instead. Tests never use a real OpenAI key (`OPENAI_API_KEY` defaults to a dummy value).

After changing a model, add an Alembic migration in `db/alembic/versions/` and confirm `make check-migrations` (`alembic check` against your migrated database) reports no drift.

Revision `11_character_bounds` enforces the public character bounds in PostgreSQL (levels 1–20, positive maximum HP and nonnegative assignment HP) and adds an index for session/event-type/id reads. It checks existing rows before making changes and refuses invalid data with counts and constraint names. Correct any invalid historical rows explicitly before retrying; this migration does not clamp or delete them. Run migration upgrade/downgrade verification on a disposable database, then use `make migrate` or `make dev-migrate` for the selected running configuration.

## CI

`.github/workflows/ci.yml` runs on pushes and PRs to `main`:

* **edgar-ruff**: `uv sync --locked --group dev`, then `ruff check .` (rules `E,F,I,B,UP`; `audit/`, `plans/` and `frontend/` are excluded). Formatting is not enforced yet.
* **edgar-tests**: the pytest suite with `CI=true` (missing Docker fails the job).
* **edgar-migrations**: against a Postgres service container, `alembic upgrade head`, `alembic check` (models vs migrations drift), and a downgrade/upgrade of the latest revision.
* **docker-build**: builds the `api` and `migrate` Docker targets.
* **frontend**: `npm ci`, `npm run lint`, `npm test --if-present`, `npm run build`.

CI pins uv to the version that writes `uv.lock`; regenerate the lock with that version (`uv lock`).
