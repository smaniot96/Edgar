# Capacity sizing: pools, queues and resources (EDG-071)

This document records the limits chosen from the offline capacity harness and what they do and do
not establish. It is a fixture-based sizing decision, not a production benchmark.

## What was measured, and what was not

Measured by `tools/capacity_harness.py` (offline, deterministic in structure, no Docker, no live
provider, no database):

- Database connection demand, held connections per unit of work, and pool queueing, by counting
  every session the real code opens. A session holds a connection from its first statement until
  commit, rollback or close.
- Whether any unit of work holds a connection while one of its own provider calls is pending.
- Concurrent provider calls in flight, per unit and in total.
- Turn latency, time to first text, and event-loop lag, at concurrency 1, 2, 4, 8, 16 and 32.

Not measured, and not established by this work:

- Real database latency. Statement cost is a modelled 2 ms per statement. The loader and persist
  transactions in the harness are stand-ins with the real statement counts, not the real queries.
- Real provider latency. Provider calls are fixed fixture sleeps (40 ms per call, 8 ms per chunk).
- Container memory or CPU. No container was started. The memory figures quoted below come from the
  14 October playtest sampling, which is a sample and can miss spikes; they are not limits.
- Redis or Qdrant latency under load, uvicorn worker scaling, TLS, network, or the real corpus size.
- Production throughput. Latencies in the table are relative fixture behaviour only.

Reproduce the harness with:

```
.venv/bin/python -m tools.capacity_harness --out "$TMPDIR/capacity-result.json"
.venv/bin/python -m pytest tests/test_capacity_harness.py -q
```

The JSON result has a stable schema (`edgar.capacity_harness/1`). The default output is under the
system temp directory and is never written to the repository.

## Results (harness, fixture timings, single process)

Final run on the committed code: `runner` = real SSE turn runner and LangGraph combat path;
`route` = real `POST /api/sessions/{id}/turn/stream` route; `mixed` = route plus the real
background summary per turn (worst case, every turn) plus a readiness-probe loop; `summary` = real
`maybe_update_memory_summary` jobs; `generation` = real `_expand_chapter` fan-out for six-chapter
generations. Pool cap 15 (SQLAlchemy defaults: `pool_size` 5 + `max_overflow` 10). All N units start
together, which is the worst case for overlap.

| scenario | N | demand | held (max) | per unit | queued acquires | wait max ms | provider in flight | viol. | fail |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| runner | 1 | 1 | 1 | 1 | 0 | 0.0 | 1 | 0 | 0 |
| runner | 8 | 8 | 8 | 1 | 0 | 0.0 | 8 | 0 | 0 |
| runner | 16 | 16 | 15 | 1 | 1 | 5.1 | 16 | 0 | 0 |
| runner | 32 | 30 | 15 | 1 | 15 | 6.5 | 32 | 0 | 0 |
| route | 8 | 8 | 8 | 1 | 0 | 0.0 | 8 | 0 | 0 |
| route | 16 | 16 | 15 | 1 | 1 | 18.9 | 16 | 0 | 0 |
| route | 32 | 32 | 15 | 1 | 17 | 54.0 | 32 | 0 | 0 |
| mixed | 8 | 9 | 9 | 1 | 0 | 0.0 | 8 | 0 | 0 |
| mixed | 16 | 17 | 15 | 1 | 2 | 20.3 | 16 | 0 | 0 |
| mixed | 32 | 33 | 15 | 1 | 21 | 55.6 | 32 | 0 | 0 |
| summary | 8 | 8 | 8 | 1 | 0 | 0.0 | 8 | 0 | 0 |
| summary | 16 | 16 | 15 | 1 | 1 | 2.5 | 16 | 0 | 0 |
| summary | 32 | 32 | 15 | 1 | 17 | 49.3 | 32 | 0 | 0 |
| generation | 1 | 0 | 0 | 0 | 0 | 0.0 | 6 (6 per generation) | 0 | 0 |
| generation | 32 | 0 | 0 | 0 | 0 | 0.0 | 192 (6 per generation) | 0 | 0 |

- `viol.` counts connections held while a provider call of the same unit was pending. It is zero at
  every level in every scenario, and a negative control in the test suite shows the detector fires.
- `per unit` is the largest number of connections any single turn, summary job or generation held
  at once. It is 1 everywhere: a turn never holds more than one connection.
- Turn latency at N=32 (p95): runner 214 ms, route 268 ms. Time to first text (runner, p95): 133 ms
  at N=1, 174 ms at N=32, with 40 ms per provider call. First text is roughly three provider waits
  (parse, adjudication, first chunk), so it is bounded by provider time, not by pool state.
- Event-loop lag p95 is about 4 to 6 ms at N=32 for turns and mixed load. The summary N=32 row
  showed one 44 ms lag sample in the final run and 0.66 ms in an earlier run; treat that as scheduler
  noise, not a characterised result.
- Demand does not grow with provider delay. The test suite runs the same N at 2 ms and 60 ms
  provider delay and checks that demand is unchanged within one connection.

## Findings

1. **No connection is held across a provider wait.** The turn route acquires its request session,
   runs the initial-state reads, commits, and only then starts the provider stream
   (`apps/api/routers/session.py`, `_load_turn_initial_state` ends with `commit()`). `persist_turn`
   opens its own short session after the stream. The summary updater reads a snapshot in one short
   transaction, calls `summarize_story` with no transaction open, and publishes in a second short
   transaction (`apps/api/services/session_messages.py`). This closes the EDG-011 and DB-D3 concern as
   written. The harness confirms it structurally, under fixture timings.

2. **Connection demand equals the number of units in a database phase.** A turn's database phase is
   its loader (about ten statements) plus its persist (about four). Demand therefore rises with the
   number of turns that are inside those phases at the same moment, not with provider time. With all
   units starting together, demand equals N. Real arrivals spread out, so real demand is lower; this
   is the worst case used for sizing. The average load is Little's law: average held connections are
   the arrival rate times the per-turn database hold time. The peak uses the simultaneous worst case.

3. **The default pool (15) saturates at about 15 concurrent database phases.** Above that, sessions
   queue for a connection. At modelled statement costs the waits are tens of milliseconds, far below
   the 30 s `pool_timeout`. The saturation is a real failure mode in production: beyond the pool,
   requests wait up to 30 s and then fail. Admission control has to refuse work before the pool runs
   out. Admission control is not implemented here (see EDG-066).

4. **Summary jobs are not bounded.** Every turn that qualifies starts a summary job, and nothing
   limits how many run together. In the harness, 32 summary jobs fill the pool and queue 17 of them.
   A bound is needed (see the recommended defaults).

5. **Generation fan-out is bounded per generation only.** Each generation keeps six chapter calls in
   flight (the semaphore in `campaign_generator`), so N generations keep 6N provider calls in flight
   and nothing bounds the total. Generation does no database work in this path, so it does not
   affect the pool.

6. **The readiness probe holds one connection across its Redis and Qdrant checks.** `/ready` runs its
   two database checks on the request session and does not commit before the Redis and Qdrant checks
   run in parallel (`apps/api/routers/health.py`). The session, and so the connection, stays checked
   out until those checks finish, up to the two-second `READY_TIMEOUT_SEC` each. This costs one
   connection per probe. Not changed here; a `rollback()` after the database checks would release it
   (proposed, see below).

## Pool formula

Let C be the pool capacity (`pool_size + max_overflow`), K the maximum number of turns admitted at
once, S the number of summary jobs that can run at once, P the number of readiness probes that can
run at once, and H the headroom kept free. Then:

    peak_connection_demand  ~=  K + S + P        (worst case, simultaneous database phases)
    requirement             :   K + S + P + H <= C

With C = 15, S = 2 (proposed bound), P = 1 (one probe at a time) and H = 2, the admitted turn limit
is K <= 10. The recommended default is K = 8, which leaves two connections of spare headroom beyond
the worst case.

## Decisions

### Database pool: validated, not changed

`db/postgres/session.py` creates the engine with SQLAlchemy defaults (`pool_size` 5,
`max_overflow` 10, `pool_timeout` 30 s, `pool_pre_ping` True). The current expected load is about
three players, so peak demand is about 3 to 5 connections, well inside 15. The default pool is not
demonstrably wrong for the supported load, so it is not changed. It is validated for up to about 10
concurrent turns with two summary jobs and one probe running alongside. Above that, admission control
(EDG-066) must refuse work rather than letting requests queue for 30 s.

Proposed, not applied: a shorter `pool_timeout` (about 5 s) so that saturation fails a turn quickly
instead of holding it for 30 s, once admission control exists.

### Recommended defaults for EDG-066 (inputs only, not implemented here)

| limit | recommended default | why |
| --- | ---: | --- |
| max concurrent turns per process | 8 | pool arithmetic: 8 + 2 + 1 + 2 headroom = 13 <= 15 |
| max concurrent summary jobs per process | 2 | bounds S in the pool formula; today unbounded |
| max concurrent generations per process | 2 | caps chapter calls in flight at 12 (2 x 6) instead of 6N |
| max concurrent provider calls (generation, global) | 12 | follows from the generation limit; a provider cost and rate judgement, not a measured capacity |

These numbers come from the pool arithmetic and the harness structure. They are not measurements of
provider rate limits or cost, which were not observed. Revisit them when the staging measurements
below exist.

### uvicorn workers: keep one

The Dockerfile runs `uvicorn apps.api.main:app` with the default single worker. Keep it there. The
preparation semaphores, the generation tasks and the Redis client are per process, and every worker
opens its own pool. Scaling out multiplies the pool demand: `workers x C` must stay below the
PostgreSQL `max_connections` (default 100) minus a reserve for administration. Add workers only after
per-process admission limits exist.

### Redis and Qdrant client timeouts: proposed, not applied

- Redis: `Redis.from_url(REDIS_URL, decode_responses=True)` in `apps/api/dependencies.py` has no
  socket timeout (`socket_timeout` and `socket_connect_timeout` default to None). A stalled Redis can
  block a turn lock call indefinitely. Proposed: `socket_timeout=2.0`, `socket_connect_timeout=2.0`,
  `health_check_interval=30`. The 2 s bound is below the 10 s lock renewal interval and the 30 s lock
  TTL, so a stall cannot silently outlive the lock. Unmeasured.
- Qdrant: `QdrantClient(url=...)` in `db/vector/client.py` takes the library default timeout (None).
  Proposed: an explicit `timeout=5`. Unmeasured.
- Provider calls already carry per-profile timeouts (60 s default in the base profile) and retry
  counts in `core/src/edgar_core/model_profiles.py`. Not changed, and not measured here.

### Container memory and CPU limits: proposed, UNMEASURED

No container was measured, so none of the following is applied to `docker-compose.yml`. The sampled
memory figures are from the 14 October playtest and are context only.

| service | sampled memory (playtest, not a limit) | proposed mem_limit | proposed cpus | status |
| --- | ---: | ---: | ---: | --- |
| api | about 584 MiB | 1g | 1.0 | UNMEASURED |
| db (postgres:16) | about 97 MiB | 512m | 1.0 | UNMEASURED |
| redis | about 76 MiB | 256m | 0.5 | UNMEASURED |
| qdrant | about 1,004 MiB | 2g | 1.0 | UNMEASURED, depends on corpus size |

The CPU cap on the API keeps it from starving Postgres and Qdrant on a shared host. The memory
headroom is roughly 1.7x for the API and 2x for Qdrant over the sampled values. Neither is a measured
peak under load. Qdrant memory scales with the indexed corpus, which was not measured.

## Operator measurements to run later (staging, not run here)

Run these against a staging stack that is not serving real players, and do not use the live checkout.
These are read-only commands. Any load that calls a real provider needs an explicit budget and
approval first.

1. Sample container resources during a load of N concurrent turns:
   `docker stats --no-stream --format '{{.Name}} {{.MemUsage}} {{.CPUPerc}}'`, repeated once per second
   into a log file.
2. Sample connection occupancy during the same load (read-only):
   `docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "select state, count(*) from pg_stat_activity where datname = current_database() group by state;"`
   Check that the count of `idle in transaction` sessions stays near zero during provider waits.
3. Sample readiness latency: `curl -s -o /dev/null -w '%{http_code} %{time_total}\n' http://127.0.0.1:8000/ready`.
4. Confirm the server limit: `docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "show max_connections;"`.
5. Drive N concurrent turns through `POST /api/sessions/{id}/turn/stream` with a stub or an approved
   provider budget. This load driver does not exist yet and would be a new tool.

What would change the numbers:

- Peak `active` plus `idle in transaction` above about K + S + P + H during the load: lower K or raise
  C.
- A loader or persist duration well above the modelled 20 to 30 ms: the per-turn hold grows, so the
  same K needs more connections on average. Recompute with Little's law.
- API memory above about 900 MiB under load, or API CPU at its cap for sustained periods: raise the
  limit or reduce K.
- Qdrant memory growing past 1.8 GiB with the real corpus: raise its limit before any corpus scaling.
- Turn p95 latency rising with N while the pool is not saturated: the bottleneck is in-process
  (event loop or provider throughput), not the pool.

## Not verified here

- Container limits and resource usage under load (no container runs).
- Real database statement latency and the real loader and persist queries (the harness uses stand-ins
  with the same statement counts).
- Real provider latency, rate limits and cost.
- Redis and Qdrant behaviour under load, and the effect of the proposed client timeouts.
- Multi-worker behaviour and any effect of a shared PostgreSQL `max_connections` limit.
- Full-scale corpus behaviour.
