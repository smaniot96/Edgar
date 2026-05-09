# Edgar

A solo Dungeon Master AI that runs a D&D 5e campaign and lets you play it from a chat UI in your browser. PostgreSQL holds all truth; an LLM proposes outcomes, the rules engine and dice tool decide them, and the database persists what happened.

## Stack

Python 3.12 with `uv` workspaces. FastAPI for the HTTP layer; Pydantic v2 schemas mirror SQLAlchemy 2.0 async ORM models. Postgres 16 stores users, campaigns, sessions, characters, NPCs, world flags, combat state, and an append-only event log; Alembic owns migrations. Qdrant stores embedded chunks of the rulebooks (PHB, DMG, MM) and the adventure module (Chalice of the Mountain God by default), with `text-embedding-3-small`. The agent is a LangGraph state machine (input parser, world retriever, rules adjudicator, world state updater, narrator, memory summarizer) with a combat subgraph that is invoked when intent classifies as combat. Redis carries a per-session turn lock so two requests cannot race a turn.

The chat UI is a single static `index.html` mounted at `/ui` by the API. It calls `POST /api/seed` once for a default campaign + character + session, then streams turns via SSE.

## Quick start

```
cp .env.example .env          # set OPENAI_API_KEY
make up                        # docker compose up + migrate + seed
open http://localhost:8000/ui
```

`make up` brings the stack up, runs Alembic, polls `/health` until the API answers (default 60s; override with `WAIT_TIMEOUT_SEC`), then hits `/api/seed`. Use `make logs` to tail the API container, `make down` to stop.

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
  apps/
    api/             FastAPI: routers, schemas, services, static UI
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
  Dockerfile         Multi-stage: base / migrate / api / ingestion / rules_engine
  docker-compose.yml api + db + redis + qdrant + migrate + ingestion + rules_engine
  pyproject.toml     uv workspace root
  Makefile           up / down / logs / migrate / seed / ingest / test
```

`db/`, `tools/`, `core/`, and the four apps are independent uv workspace members; the workspace root lockfile is the only `uv.lock` you should regenerate.

## Configuration

`config.py` lives in `core/src/edgar_core/config.py`. It reads component env vars (`DB_HOST`, `POSTGRES_USER`, etc.) and builds the URLs the rest of the code consumes (`DATABASE_URL`, `REDIS_URL`, `VECTOR_DB_URL`). Docker overrides only host names so the same `.env` works on the host (via `localhost`) and inside Compose (via `db`, `redis`, `qdrant`). `LLM_MODEL` and `EMBEDDING_MODEL` are env-overridable; defaults are `gpt-4o-mini` and `text-embedding-3-small`.

## Tests

```
make test
```

The suite spins up Postgres 16 via testcontainers, fakes Redis with `fakeredis`, fakes the OpenAI LLM with a deterministic stub, and runs the full turn flow end to end. Docker must be reachable. CI runs the same suite on GitHub Actions; the workflow is in `.github/workflows/ci.yml`.
