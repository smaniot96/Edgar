# Edgar — Platform / SRE Production-Readiness Review

Reviewer role: senior platform/SRE engineer. Scope: tests, CI, Docker/compose, config, observability, migrations, cost/reliability of the LLM turn pipeline. Review was **read-only**.

Repo: `/Users/psmaniotto/Desktop/MyProjects/Edgar` (branch `dev`, main = `production`).

---

## TL;DR verdict

The app is well-structured for a prototype: clean config seam, transactional world-writes, request-id correlation, structlog JSON in non-local, a Redis turn lock, and consistent migrations. **It is not production-ready** primarily because of reliability and cost exposure on the OpenAI path (no timeouts, no retries, no rate-limit handling, unbounded fan-out of calls per turn), zero test coverage of the combat path and SSE path, a single CI test job with no real failure-mode coverage, and operational gaps in the compose stack (no restart policies, no resource limits, no API healthcheck, containers run as root, no DB connection-pool tuning).

---

## LLM calls per turn (cost model)

Counted across `input_parser`, `world_retriever` (no LLM — vector search only), `rules_adjudicator`, `world_state_updater` (no LLM — DB only), `narrator`, `memory_summarizer`, and combat enemy actions.

### RP / exploration turn (non-combat) — `apps/api/services/turn_runner.py` and `apps/agent/src/agent/graph.py`
| Step | LLM call? | Notes |
|---|---|---|
| input_parser | 1 | `with_structured_output(ParsedInput)` — `apps/agent/src/agent/nodes/input_parser.py:28` |
| world_retriever | 0 | Qdrant only (2 vector searches), no chat model |
| rules_adjudicator | 1 | `with_structured_output(AdjudicationResult)` — `rules_adjudicator.py:57` |
| world_state_updater | 0 | DB telemetry write only |
| narrator | 1 | streamed `astream` (SSE) or `ainvoke` (sync) — `turn_runner.py:196`, `narrator.py:84` |
| memory_summarizer | 0 or 1 | Fires only when `len(messages) >= 10` — `memory_summarizer.py:24` |

**Total: 3 OpenAI chat calls per normal turn (4 every ~Nth turn when summarization triggers), plus 2 embedding-driven vector searches.**

### Combat turn — `apps/agent/src/agent/graph_combat.py`
The combat subgraph runs **one initiative actor per player turn** (`combat_turn_node`). Per invocation:
| Step | LLM calls |
|---|---|
| `_get_turn_input` (enemy action) | 1 **if the active actor is an enemy** (`graph_combat.py:77`); 0 if it's the player's slot |
| input_parser (re-run inside subgraph on the turn input) | 1 (`combat_turn_node` loops the full node sequence — `graph_combat.py:96`) |
| rules_adjudicator | 1 |
| narrator | 1 |
| memory_summarizer (main graph, after combat) | 0 or 1 |

**Total: 3 chat calls when it's the player's slot; 4 chat calls when the active actor is an enemy (extra enemy-action call). Plus the input_parser ran once already in `turn_runner` before routing into the combat graph — see Finding C-1: the parser runs TWICE on a combat turn.**

> Cost note: a single combat encounter of 10 rounds with one enemy = ~20 actor-turns, each a separate HTTP turn from the client, each 3–4 chat calls = **60–80 OpenAI calls per encounter**, none cached, none retried, none rate-limited. At `gpt-4o-mini` defaults this is cheap-ish, but with a larger `LLM_MODEL` override it scales linearly and silently.

---

## CRITICAL findings

### C-1. No timeouts, retries, or rate-limit handling on any OpenAI call
**Severity: CRITICAL**
**Files:** `apps/agent/src/agent/llm.py:7-12`; every node (`input_parser.py:24`, `rules_adjudicator.py:53`, `narrator.py:79`, `memory_summarizer.py:31`, `graph_combat.py:77`)
**Description:** `make_chat_model` constructs `ChatOpenAI(model, temperature, api_key)` with **no `request_timeout`, no `max_retries`, no `default_headers`**. `grep` for `tenacity|max_retries|request_timeout|retry` across `apps/` and `core/` returns nothing operational. Failure modes in production:
- OpenAI 429 (rate limit) or 5xx → the exception bubbles up; nodes catch it and return `{"error": str(e)}`, which the API turns into a **503 with the raw provider error string in the response body** (`session.py:226`). No backoff, no retry, the turn is lost.
- A hung OpenAI connection has **no timeout** → the request (and the held Redis turn lock, TTL 60s) can stall until the client or proxy gives up. `langchain_openai` defaults exist but are not pinned, so behavior is version-dependent and undocumented.
- Quota exhaustion (`insufficient_quota`) → every turn fails hard; there is no degraded mode and no alerting hook.
**Recommended fix:** Set explicit `timeout=` and `max_retries=` on `ChatOpenAI` (LangChain has native exponential backoff via `max_retries`). Wrap calls in `tenacity` with jittered backoff for 429/5xx, surface a user-friendly "the DM is thinking too hard, try again" on exhaustion, and emit a structured log/metric on each retry and final failure. Pin `langchain-openai`/`openai` versions.

### C-2. Raw provider/exception strings leaked to clients
**Severity: CRITICAL (security + ops)**
**Files:** `apps/api/routers/session.py:226`, `:237`, `:266`; `turn_runner.py:139,164,173,180,203,229` (SSE `error` frames carry `state["error"]` / `str(e)`)
**Description:** The global handler in `main.py:86` correctly returns a generic "Internal server error" for unhandled 500s, **but the turn endpoints bypass it** by explicitly raising `HTTPException(status_code=503, detail=result["error"])` and `HTTPException(500, detail=str(e))`, and the SSE runner emits `error` frames containing the raw error. OpenAI errors can include the org/project and API details; SQLAlchemy/asyncpg errors can include SQL fragments and connection strings. This leaks internals and undermines the deliberate redaction in the global handler.
**Recommended fix:** Return generic messages with the `request_id` (mirroring `main.py`), log the detail server-side at `error` level. Map known errors (rate-limit, quota, validation) to stable client-safe codes.

### C-3. SSE turn runner mutates and commits the request-scoped DB session inside a long-lived stream
**Severity: CRITICAL (data integrity / connection exhaustion)**
**Files:** `apps/api/routers/session.py:299-311`, `apps/api/services/turn_runner.py:104,162,217`
**Description:** The streaming endpoint holds the `get_session` DB connection (`db.postgres.session.get_session`, default pool) open for the **entire duration of the SSE stream** — which spans multiple OpenAI calls and token streaming. Under concurrency this pins one DB connection per active stream for tens of seconds. Combined with **no pool tuning** (Finding O-6), a handful of concurrent players can exhaust the default SQLAlchemy pool (size 5 + overflow 10) and block all other requests. Additionally, if the client disconnects mid-stream, the `finally` releases the Redis lock but the partial transaction state and any bound contextvars depend on generator finalization, which is not guaranteed promptly on disconnect.
**Recommended fix:** Acquire a short-lived DB session only at the persist step (open in `_persist_turn`, commit, close), not for the whole stream. Do not hold a pooled connection across LLM I/O. Add disconnect handling.

---

## HIGH findings

### H-1. Combat path and SSE path are entirely untested
**Severity: HIGH**
**Files:** `tests/` (30 test functions total). `grep "combat" tests/` matches only `conftest.py`. No test exercises `graph_combat.py`, the combat subgraph, initiative, round capping, `combat_turn_node`, `persist_combat`, or `stream_session_turn` / the SSE event protocol.
**Description:** The most complex and stateful code (combat round-robin, initiative persistence, combat-ends detection, enemy-action LLM call, double-parse) has **zero coverage**. The `fake_llm_turn` fixture only models a single non-combat run (`ParsedInput(intent="exploration")`) and explicitly `raise AssertionError` for any structured schema it doesn't expect — it cannot drive a combat turn at all. The SSE contract (`status`/`token`/`adjudication`/`done`/`error` frames) documented in `turn_runner.py` is unverified.
**Recommended fix:** Add a combat fake LLM (intent="combat", deterministic enemy actions) and tests for: initiative ordering/persistence, round increment + `MAX_COMBAT_ROUNDS` cap, "combat ends" narrative termination, and `persist_combat` insert-vs-update by `id`. Add an SSE test asserting frame ordering and the `done` payload.

### H-2. CI does not gate on combat/integration, runs no type-check, no security/dependency scan, no Docker build
**Severity: HIGH**
**File:** `.github/workflows/ci.yml`
**Description:** CI runs three jobs: `ruff check`, `pytest tests/ -x --tb=short`, and a frontend `npm run build`. Gaps:
- `pytest -x` stops at first failure (fine for speed but hides breadth).
- The Postgres testcontainers fixture **silently `pytest.skip`s if Docker is unavailable** (`conftest.py:57-58`). On a GitHub runner Docker is present, but there is no assertion that the DB-backed tests actually ran — a misconfiguration would skip them green.
- No `mypy`/`pyright` despite heavy typing and Pydantic models.
- No dependency vulnerability scan (`pip-audit`/`uv`-based), no SAST, no secret scan.
- **The Docker images are never built or smoke-tested in CI**, so a broken `Dockerfile`/compose (the actual deploy artifact) ships undetected. (Note recent commit `47358b3 fix(frontend): regenerate package-lock for Docker npm ci` — exactly the class of break CI doesn't catch.)
- No coverage threshold.
**Recommended fix:** Add `docker build` for each target + a compose smoke test (bring stack up, hit `/ready`), add a type-check job, add `pip-audit` and a secret scanner, fail CI if testcontainers skipped, and report coverage with a floor.

### H-3. No restart policies on app/data services in compose
**Severity: HIGH**
**File:** `docker-compose.yml` (only `migrate` has `restart: "no"`; `api`, `db`, `redis`, `qdrant` have none → default `no`)
**Description:** With the default restart policy, a crashed `api`, `db`, `redis`, or `qdrant` stays down until manual intervention. For a long-running service this is an availability defect.
**Recommended fix:** `restart: unless-stopped` (or `on-failure`) for `api`, `db`, `redis`, `qdrant`. Keep `migrate` at `no` (one-shot).

### H-4. No API healthcheck in compose; `/ready` exists but is unused by orchestration
**Severity: HIGH**
**Files:** `docker-compose.yml:21-40` (api has `depends_on` but **no `healthcheck`**); `apps/api/routers/health.py`
**Description:** `db`, `redis`, `qdrant` have healthchecks, but the `api` service does not, so nothing downstream (a reverse proxy, an orchestrator, or `depends_on` of another service) can gate on API readiness. `/ready` checks Postgres (`SELECT 1`) but **not Redis or Qdrant**, both of which are hard dependencies of a turn — a turn will 503/500 on a degraded Redis/Qdrant even though `/ready` says ready. `/health` is a static `{"status":"ok"}` (fine as liveness).
**Recommended fix:** Add an `api` healthcheck hitting `/ready`. Extend `/ready` to verify Redis (`PING`) and Qdrant reachability so readiness reflects true turn-serving capability.

### H-5. Containers run as root; no non-root `USER`
**Severity: HIGH (security)**
**File:** `Dockerfile` (no `USER` directive in any stage)
**Description:** All stages (`api`, `migrate`, `ingestion`, `rules_engine`) run as root. A compromise via a dependency or the LLM-driven path runs with root in-container.
**Recommended fix:** Create and switch to a non-root user in the `base`/`api` stages; ensure `/app` and the uv venv are owned appropriately.

### H-6. Cost/abuse exposure: no rate limiting, no per-user quotas, single hard-coded user
**Severity: HIGH**
**Files:** `apps/api/dependencies.py:25-38` (`current_user_id` returns first user — "single-user seam"); no throttling anywhere; turn lock is per-session only.
**Description:** Any caller can POST turns as fast as they like (only blocked per-session by the Redis lock). Each turn is 3–4 paid OpenAI calls. With no auth, no per-IP/per-user rate limit, and no spend cap, this is an open door to runaway OpenAI cost in production. The `current_user_id` single-user shortcut also means there is **no real authentication** on any endpoint.
**Recommended fix:** Add authentication, per-user/per-IP rate limiting, and a budget guard (daily token/cost ceiling with a circuit breaker). Do not deploy publicly with the single-user seam.

---

## MEDIUM findings

### M-1. input_parser runs twice on every combat turn
**Severity: MEDIUM (cost + latency)**
**Files:** `apps/api/services/turn_runner.py:136` (parser run), then `:150` `app.ainvoke(...)` re-enters the graph whose first node is `input_parser` (`graph.py:36,44`), and inside combat `combat_turn_node` runs `input_parser_node` **again** on the turn input (`graph_combat.py:96`).
**Description:** On a combat turn the player message is parsed in the runner, then `app.ainvoke` runs the graph from `input_parser` again (parse #2), and the combat subgraph parses the turn input (parse #3 for the player slot). That's redundant paid calls and extra latency. The sync endpoint (`session.py:223`) also re-parses since it calls `app.ainvoke` on state that the SSE path would have parsed — but the sync path doesn't pre-parse, so it's the SSE/combat interaction that double-pays.
**Recommended fix:** Thread the already-parsed `parsed_input` into the graph state so downstream entry skips re-parsing; have `combat_turn_node` reuse the parse for the player slot.

### M-2. `world_state_updater_node` opens its own DB session/transaction outside the request transaction
**Severity: MEDIUM (consistency)**
**File:** `apps/agent/src/agent/nodes/world_state_updater.py:24-32`
**Description:** The telemetry event_log write commits in a **separate** short-lived session (`async_session_factory()`), independent of the API's request transaction that does the source-of-truth writes. If the request transaction later rolls back (e.g. `MissingCharacterAssignmentError`), the telemetry row is already committed → event_log can contain an `adjudication` event for a turn whose game-state writes never landed. Also adds a second DB connection acquisition per turn (pool pressure, see O-6).
**Recommended fix:** Either pass the request session into the node, or accept the inconsistency explicitly and document it; at minimum tag the row so it can be reconciled.

### M-3. `MAX_COMBAT_ROUNDS` is the only loop guard; "combat ends" detection is brittle substring match
**Severity: MEDIUM**
**File:** `apps/agent/src/agent/graph_combat.py:115-120`
**Description:** Combat ends either at round 10 or when the adjudicator's `mechanical_summary` contains the literal substring `"combat ends"` (case-insensitive). An LLM phrasing it differently ("the battle is over", "the goblin falls and the fight is done") will not end combat; conversely a stray mention could end it early. There is no HP-based termination (enemies aren't tracked with HP).
**Recommended fix:** Add a structured `combat_ended: bool` field to `AdjudicationResult` (or track enemy HP) rather than string-matching free text.

### M-4. Migrations: downgrade of `8_character_assignments` is data-lossy and likely fails on real data
**Severity: MEDIUM**
**Files:** `db/alembic/versions/8_character_assignments.py:110-144`, `1ad2ad6560f5_...py:37` (re-adds `characters.session_id NOT NULL` with no default on downgrade)
**Description:** The migration chain is **linear and consistent** (single head `8_character_assignments`, clean `down_revision` links) — good. But several downgrades are unsafe: `8`'s downgrade drops `character_assignments` and re-adds non-nullable columns to `characters` then back-fills via `op.execute` from assignments it is about to lose; `1ad2ad6560f5` downgrade re-adds `characters.session_id` as `NOT NULL` with no server default, which will fail on any non-empty table. Upgrades look fine; downgrades are effectively one-way in production.
**Recommended fix:** Treat downgrades as best-effort and document them as non-production. If reversibility matters, add server defaults / nullable-then-backfill-then-alter patterns and guard back-fills.

### M-5. `migrate` is a one-shot `depends_on: service_completed_successfully` — no leader election / concurrency guard for multi-replica
**Severity: MEDIUM**
**Files:** `docker-compose.yml:8-19,34-36`; `Dockerfile:16-17`
**Description:** The single-node compose ordering (migrate → api) is correct for one host. But the pattern (run `alembic upgrade head` as a container that the API waits on) does not generalize to a multi-replica/cloud deploy where two API instances could each trigger migrations. Alembic itself takes a lock, but the compose flow has no notion of "run once across the fleet."
**Recommended fix:** For cloud, run migrations as an explicit pre-deploy job/step (not per-replica). Document this; the current setup is single-host only.

### M-6. Secrets and config delivered via a single `.env` mounted into every service
**Severity: MEDIUM (security/ops)**
**Files:** `docker-compose.yml` (`env_file: .env` on every service), `core/src/edgar_core/config.py:42`
**Description:** `.env` is correctly gitignored (`.gitignore:70-72`, not tracked, not in history). But `OPENAI_API_KEY` and DB creds are shipped via a flat `.env` to all containers including `ingestion` and `rules_engine` that may not need the OpenAI key. No secrets manager, no per-service scoping, defaults bake in `user/password/dnd` (`config.py:24-26`, compose `:83-85`).
**Recommended fix:** In cloud, source secrets from a manager (and not the same `.env` for every service). Fail fast if `OPENAI_API_KEY` is unset at API startup rather than per-turn (`input_parser.py:21` returns an error only at turn time).

### M-7. `/ready` only checks Postgres; turn-critical Redis & Qdrant unchecked (see also H-4)
Covered above; called out separately because the readiness signal is misleading for the actual SLO (serving a turn).

### M-8. No metrics / instrumentation
**Severity: MEDIUM (observability)**
**Files:** entire `apps/` — structlog is configured (`logging_config.py`) and request-id correlation is good (`main.py:49-66`), but there is **no metrics export** (no Prometheus, no OpenTelemetry, no per-turn LLM latency/token/cost counters).
**Description:** In production you cannot answer: turns/min, p95 turn latency, OpenAI error rate, tokens/cost per turn, lock-contention rate (409s), or Qdrant latency. Logs alone won't drive alerting/SLOs.
**Recommended fix:** Add OpenTelemetry (FastAPI + httpx auto-instrumentation) and explicit counters/histograms for LLM calls (count, latency, tokens, failures by reason) and turn outcomes.

---

## LOW findings

### L-1. `LLM_MODEL` / `EMBEDDING_MODEL` are unvalidated env strings
**Severity: LOW**
**File:** `core/src/edgar_core/config.py:49-50` — a typo'd model name fails only at first OpenAI call (per-turn 503), not at startup.
**Fix:** Validate/log the resolved model at startup.

### L-2. `temperature=0.7` hard-coded in three places (narrator, combat enemy action, turn_runner)
**Severity: LOW**
**Files:** `narrator.py:20`, `graph_combat.py:77`, `turn_runner.py:41` — duplicated magic number; drift risk between streamed and non-streamed narrator.
**Fix:** Centralize narration/temperature settings in config.

### L-3. `docker-compose` exposes Postgres (5432), Redis (6379), Qdrant (6333) on the host
**Severity: LOW (dev-convenience, but risky if reused in prod)**
**File:** `docker-compose.yml:88-89,103-104,114-115` — fine locally; in any shared/cloud host this exposes unauthenticated Redis/Qdrant and the DB.
**Fix:** Don't publish data-service ports in production compose; keep them on the internal network only.

### L-4. No `.dockerignore` audit of build context size / `COPY . .`
**Severity: LOW**
**File:** `Dockerfile:13` `COPY . .` copies the whole repo into every stage. A `.dockerignore` exists; verify it excludes `.venv`, `data/`, `node_modules`, caches to keep images small and avoid leaking local artifacts.
**Fix:** Confirm `.dockerignore` covers `.venv/`, `data/`, `.pytest_cache`, `.ruff_cache`, `frontend/node_modules`.

### L-5. Memory summarizer threshold/keep values hard-coded; no token-based bound
**Severity: LOW**
**File:** `memory_summarizer.py:13-15` — fixed message counts, not token budgets; a few very long turns can still blow the narrator context even under the threshold.
**Fix:** Bound by token count, not message count.

### L-6. Default DB credentials baked into compose and config defaults
**Severity: LOW**
**Files:** `config.py:24-26`, `docker-compose.yml:83-85` (`user`/`password`/`dnd`) — convenient for dev, dangerous if a deploy forgets to override.
**Fix:** Remove insecure defaults for cloud; require explicit values.

---

## What IS done well (for balance)

- Config seam (`core/src/edgar_core/config.py`) is clean: component env vars → URLs, one `.env`, host overrides only in compose. No URL/credential duplication.
- Migration chain is **linear and consistent** (single head, correct `down_revision` links, ordered).
- World-writes are transactional and HP is clamped to `[0, hp_max]` (`world_writes.py:75`); a dedicated test guards the HP-delta-zeroing regression (`test_turn.py:71`).
- Request-id middleware + structlog contextvars + JSON logs outside `local`, and a global exception handler that redacts internals for unhandled 500s (`main.py`).
- Per-session Redis turn lock with TTL prevents concurrent turns on one session and self-heals on crash (`dependencies.py:55-65`).
- Data persistence volumes for Postgres and Qdrant; sensible healthchecks on the three data services; correct one-shot migrate ordering for single-host.
- Test fakes are reasonable for what they cover: testcontainers Postgres with real Alembic upgrade, `fakeredis`, monkeypatched retrieval and a fake structured-output LLM.

---

## Prioritized remediation order

1. **C-1** LLM timeouts/retries/rate-limit handling (reliability + cost).
2. **C-2** Stop leaking raw error strings to clients/SSE.
3. **C-3** Don't hold a pooled DB connection across the SSE/LLM lifetime.
4. **H-6** Auth + rate limiting + spend cap before any public exposure.
5. **H-1 / H-2** Combat + SSE test coverage; build Docker images and smoke-test in CI.
6. **H-3 / H-4 / H-5** restart policies, API healthcheck + richer `/ready`, non-root containers.
7. **M-1 / M-2 / M-3 / M-8** double-parse cost, telemetry transaction consistency, structured combat-end, metrics.
8. Remaining MEDIUM/LOW hardening.
