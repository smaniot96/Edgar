# Hosting hardening (EDG-072)

This note records what the Compose files now enforce for shared or hosted deployments, what the
opt-in hosting profile changes, and what is still open. The local stack (`docker-compose.yml`
with or without `docker-compose.dev.yml`) keeps its previous ports and unauthenticated loopback
datastores. The hosting profile is opt-in and is not the same as "safe to expose".

## What is scoped in the default stack

Each service receives only the names its code reads. `env_file: .env` is gone; Compose
interpolates each allowed name from `.env` (or the shell) into that service's `environment:`.

| Service | Receives |
| --- | --- |
| `migrate` | `DB_HOST`, `DB_PORT`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` |
| `api` | database, Redis and Qdrant coordinates; `OPENAI_API_KEY`, `VECTOR_DB_API_KEY`; model settings; `ENV`; `EDGAR_DEBUG`; the metrics, trusted-price and visual-review settings it reads |
| `ingestion`, `rules_engine` | Qdrant coordinates; `OPENAI_API_KEY` (embeddings), `VECTOR_DB_API_KEY`; model settings. No Postgres credentials |
| `db` | `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` |
| `redis`, `qdrant` | nothing from `.env` |

Defaults in the allow-list equal the defaults in `edgar_core.config` and `model_profiles`, so a
missing optional value behaves as before. `tests/test_compose_hardening.py` checks the lists
against the names the code reads and fails when a service gains or loses one.

Runtime hardening for `migrate`, `api`, `ingestion` and `rules_engine`: `cap_drop: [ALL]` and
`security_opt: [no-new-privileges:true]`. Each of these images runs as the non-root `edgar` user
(`USER edgar` in the Dockerfile), binds only port 8000, and writes only to bind mounts owned by
that user, so it needs no Linux capability. The datastores are unchanged.

Settings: `REDIS_PASSWORD` (default empty) is read by `edgar_core.config`. When set, it is
percent-encoded into `REDIS_URL`. An empty value keeps the unauthenticated URL.

## What the hosting profile does

Use it with the base file only:

```
docker compose -f docker-compose.yml -f docker-compose.hosting.yml up -d
```

- No host ports for Postgres, Redis or Qdrant. The API keeps `127.0.0.1:8000`.
- Redis runs with `requirepass` from `REDIS_PASSWORD`. The API, and Redis's own healthcheck
  (`REDISCLI_AUTH`), use the same value.
- Qdrant runs with `QDRANT__SERVICE__API_KEY` from `VECTOR_DB_API_KEY`. The API, ingestion and
  rules engine send the same key.
- Postgres requires `POSTGRES_PASSWORD`. The default `password` is not reachable from this profile.
- The datastores join an `internal` network with no route out. The API, ingestion and rules engine
  also join an `egress` network because they call the provider API. `migrate` stays on the
  internal network only.
- Every credential is `${NAME:?...}`. Compose refuses to start when one is missing or empty.

The profile needs Docker Compose 2.24.4 or newer for the `!reset` tag, which clears the inherited
datastore ports.

## What is not done

- **Read-only root filesystem.** Not enabled. Writes are known for `/data`, `/index-locks`,
  `/output/markdown` (ingestion), but the code alone does not prove the rest. Before enabling
  `read_only: true`, verify in a staging run with tmpfs for `/tmp` and any other path the
  libraries use, such as the PDF and OCR temp files (PyMuPDF, Tesseract), the `edgar` home
  directory (`/home/edgar`, for any library cache), and `__pycache__` under `/app` when a module is
  first imported.
- **TLS.** The API speaks plain HTTP on loopback. A TLS-terminating proxy, outside Compose, must be
  the only public entry point.
- **Secrets manager.** Secrets still come from `.env` or the shell. `docker inspect` shows
  container environments, and the Redis password is also an argument to `redis-server` (no shell,
  so it is not expanded there). Source the values from the deployment's secret store and keep the
  Docker host restricted.
- **Network policy outside Compose.** Host firewall and cloud security groups must close every
  port except the proxy's.
- **Authentication and ownership (EDG-065).** The hosting profile does not add user
  authentication. The identity dependency still returns the first user, and some campaign routes
  lack owner checks (see the unresolved finding S1). Do not expose the API publicly until EDG-065 is
  done.
- **Resource limits (EDG-071).** Container CPU and memory limits are unmeasured. Do not apply any
  proposed limit values before they are measured.
- **Datastore images and TLS between services.** `postgres:16`, `redis:8.6.3` and
  `qdrant/qdrant:v1.16.3` are unchanged. Traffic on the internal network is plaintext. Redis and
  Qdrant use one shared secret each, with no per-role ACL or read-only key.
- **Unverified in this change.** No Docker container was started or built. The compose changes were
  checked with `docker compose config` (no daemon) and the static tests. The `stack-smoke` CI job
  is the first run against the new files. Nothing has exercised `cap_drop`, the hosting profile's
  Redis AUTH, Qdrant's API key, or the internal network.

## Pre-hosting checklist

1. Replace `POSTGRES_PASSWORD`, and set `REDIS_PASSWORD` and `VECTOR_DB_API_KEY`, from a secret
   store. Use long random values, and never commit `.env`.
2. Validate the merged config without printing it. `config` prints resolved secrets, so use
   `--quiet`:
   `docker compose -f docker-compose.yml -f docker-compose.hosting.yml config --quiet`.
3. Confirm that only `127.0.0.1:8000` is published, and that the `datastore` network is internal.
4. Deploy to a staging host first. Run the smoke path (`/ready`, `/ui/`, one seed then read) with
   the hosting profile.
5. Complete EDG-065 (authentication and owner checks) and put a TLS proxy in front of the API.
6. Measure resource use, then set limits (EDG-071).
7. Try `read_only: true` with tmpfs in staging, and keep it only if the smoke path passes.
8. Rotate the credentials before go-live, and keep the recovery procedure in
   `docs/recovery-drill.md` current.
