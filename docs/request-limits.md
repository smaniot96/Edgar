# Request, upload and spend limits

Status: EDG-066. These limits bound how much paid work and heavy I/O one API deployment will
start. They are a shared-hosting safety net, not an authorization model. Per-user fairness
needs real authentication and ownership first (EDG-065).

## Defaults and settings

Every setting is a non-secret integer read from the environment (`.env` or the process
environment). A malformed or out-of-range value stops the API at import, and the error names the
setting but never its value. Defaults are sized so one local player never notices them.

| Setting | Default | Meaning |
| --- | --- | --- |
| `LIMIT_TURNS_PER_MINUTE` | 30 | Play turns, streamed turns and intros per client per minute (fixed window). |
| `LIMIT_JOB_STARTS_PER_HOUR` | 20 | Starts of paid or heavy background work per client per hour: PDF upload, source recovery, AI generation, preparation setup, retry. |
| `LIMIT_MAX_CONCURRENT_TURNS` | 8 | Turns in flight across all replicas. |
| `LIMIT_MAX_CONCURRENT_TURNS_PER_CLIENT` | 4 | Turns in flight per client. Must not exceed the global cap. |
| `LIMIT_MAX_CONCURRENT_JOBS` | 4 | Background jobs in flight across all replicas (ingestion, embedding, generation, preparation). |
| `LIMIT_MAX_UPLOAD_BYTES` | 67108864 (64 MiB) | Largest accepted PDF. Enforced on the declared length before parsing and on the exact copied size. |
| `LIMIT_DAILY_SPEND_MICROUSD` | 50000000 (USD 50) | Advisory UTC-day threshold for estimated provider spend, in USD micro-units. |
| `LIMIT_ASSUMED_CALL_COST_MICROUSD` | 100000 (USD 0.10) | Charged for a provider call whose cost is unknown, and for each intro generation. |
| `LIMIT_UNBUDGETED_JOB_COST_MICROUSD` | 500000 (USD 0.50) | Accounting reservation for a job that declares no preparation cost target. |
| `LIMIT_TURN_LEASE_SECONDS` | 300 | Lifetime of a turn lease. A lease whose holder died is freed after this long. |
| `LIMIT_JOB_LEASE_SECONDS` | 7200 | Lifetime of a job lease. Must be at least 60. |

Set them in `.env`, for example `LIMIT_TURNS_PER_MINUTE=60`. Raising a value only widens the
bound. The validated values are logged once at startup as `limits_configured`.

## Responses

A refused request does no paid or heavy work. The body uses the shared safe-error payload, and
every refusal has the same shape:

```json
{
  "code": "rate_limited",
  "category": "rate_limited",
  "message": "The Dungeon Master service is receiving too many requests. Check the result before retrying.",
  "detail": "...same as message...",
  "recovery_action": "retry",
  "retryable": true,
  "support_ref": "err-0123456789abcdef",
  "outcome": "not_committed",
  "retry_after_seconds": 42
}
```

The response carries `Retry-After` when a rate window sets the wait.

Daily cost estimates do not refuse work. Crossing the advisory threshold emits
`daily_spend_estimate_exceeded`, and preparation continues. Retained historical error
records may still contain `daily_budget_exhausted`.

| Code | Status | When |
| --- | --- | --- |
| `rate_limited` | 429 | A per-client window is exhausted. Retry after `retry_after_seconds`. |
| `concurrency_limited` | 429 | The global or per-client concurrency cap is full. Retry shortly. |
| `upload_too_large` | 413 | The PDF exceeds `LIMIT_MAX_UPLOAD_BYTES`. |
| `upload_unsupported` | 415 | The upload is not a PDF: the content lacks a PDF header, or the declared type is not a PDF type. |
| `limits_unavailable` | 503 | The developer usage view cannot read Redis. |

No response contains a client address, a Redis key, a traceback, or provider text.

## Where limits apply

| Route | Checks, in order |
| --- | --- |
| `POST /api/sessions/{id}/turn`, `/turn/stream`, `/intro` | Per-client turn rate; advisory cost accounting; global and per-client turn leases. The lease is released on every exit, including a client disconnect. |
| `POST /api/adventures/upload` | Content-length guard (before multipart parsing); per-client job starts; job lease; PDF content and type check; copy cap; reservation of the unbudgeted cost. |
| `POST /api/adventures/generate` | Job starts; job lease; reservation of the advisory preparation cost target (or the unbudgeted cost) before any slug is created. |
| `POST /api/adventures/{slug}/preparation/setup` | Job starts; job lease; reservation of the advisory cost target. |
| `POST /api/adventures/{slug}/retry` | Job starts; job lease; reservation of the saved cost target (or the unbudgeted cost). |
| `POST /api/campaigns/{id}/sources/{slug}/upload` | Inert compatibility route: checks ownership and binding, then reports held immutable source. No job is admitted. |
| `GET /api/limits/usage` | Developer view (see below). |

A job's lease is released when its background task finishes, whether it succeeds, fails or is
cancelled. A job that never reaches its background task is undone when the request ends.

## Spend accounting

- A turn is charged its measured cost from the EDG-053 turn collector. A provider call whose cost
  is unknown, or that the collector dropped, is charged `LIMIT_ASSUMED_CALL_COST_MICROUSD`. Unknown
  spend is never counted as zero.
- An intro is charged the assumed call cost.
- A job reserves its advisory preparation cost target (`cost_target_microusd`) when it is accepted, or
  `LIMIT_UNBUDGETED_JOB_COST_MICROUSD` when it declares none. The reservation is atomic;
  exceeding the advisory threshold logs a warning and work continues. A reservation for a request that is refused or fails
  before its job starts is returned.
- A private preparation job settles its reservation when it ends in a terminal state (ready,
  failed, blocked or cancelled), whether it returned or raised (EDG-055 F6). The job's own run
  ledger is read before it starts and after it ends: the reservation is replaced by what this
  attempt spent (recorded semantic response usage), so a job that
  fails before any provider call returns its whole reservation. Spend above the reservation is
  added when the starting ledger was readable; otherwise the reservation is the most charged.
- A job whose ledger is unreadable, or which ends without a terminal state (a stale worker, a
  process stop), keeps its whole reservation. That over-counts, which is the safe direction.
  Recovery after a restart runs without a request admission, so it settles nothing.
- Repeated preparation or retry of the same adventure reserves again; each attempt settles its
  own reservation against its own share of the run ledger.

The advisory cost record is an estimate, separate from billing reconciliation.

## Principal

Per-client limits are keyed by the direct peer address (`request.client.host`). Forwarded headers
are not trusted, because a client can set them. Behind a reverse proxy every client then shares the
proxy's address, so the per-client limits collapse into one shared bucket. The global caps still
hold. Terminate clients at a proxy that you control before relying on per-client behaviour.

The single-user seam (`current_user_id`) is not used as a key. It returns the same user for every
caller, so keying by it would put all clients in one bucket. When EDG-065 adds a real identity, the
principal becomes the user id and this module changes in one place (`client_principal`).

## Redis failure policy

Counters live in Redis, so every replica agrees.

- Rate windows and concurrency leases **fail open** and log `limits_backend_unavailable` with
  `policy=fail_open`. The turn lock already needs Redis and fails closed on its own, so a Redis
  outage stops turns regardless.
- Daily cost accounting **fails open**. Missing usage counters never turn the preparation
  measurement goal into a stopping condition.
- Recording spend after work already ran **fails open**. An outage under-counts spend and logs
  `limits_spend_record_failed`.

There is no in-process fallback. The app has no in-process degradation for Redis elsewhere, so
adding one here would make replicas disagree.

## Usage view and observability

- `GET /api/limits/usage` returns the current global turn and job concurrency, today's spend and
  the remaining allowance, and the configured settings. It returns 404 unless `EDGAR_DEBUG=1`, the
  same gate as the other developer diagnostics. It never returns client identifiers.
- Structured logs: `request_limited` (scope, code, status, retry hint), `limits_configured` (startup
  values), `limits_backend_unavailable`, `limits_spend_record_failed`. Fields are fixed names and
  numbers, and no client address or secret is logged.
- For metrics (EDG-139), `apps/api/limits.add_rejection_observer(callback)` calls
  `callback(scope, code)` on every refusal. The callback must not block. It is not an import
  dependency.

## Not limited yet

- Read routes, and create, update and delete routes that spend no provider money (sessions,
  characters, NPCs, world flags, journal, campaign binding and lifecycle, seeds).
- Per-campaign budgets for multiplayer. The app is single-player, and no campaign budget exists.
- True per-user fairness. Until EDG-065 supplies identity, all clients behind a proxy share one
  per-client bucket, and the rate and concurrency caps are per IP.
- Fixed windows allow a burst of up to twice the limit across a window boundary.
- Combat enemy-action calls within one turn beyond the existing `MAX_COMBAT_ROUNDS` cap, and the
  total number of sessions a client may create.
- Request bodies that arrive without a Content-Length are parsed before the post-parse copy cap
  refuses them. Set a body-size limit at the reverse proxy as well.
- A turn that runs longer than `LIMIT_TURN_LEASE_SECONDS` is counted free for the rest of its run.
  Raise the setting if long turns are expected.
- Settlement for legacy (non-private) ingestion and generation jobs, which keep their
  reservation, and per-model spend breakdowns.
- Durable job queueing and restart recovery (T1). Leases limit concurrency, not durability.

## Verification

`tests/api/test_request_limits.py` covers the rate window (trip and recovery), the concurrency caps
with release on success, error and cancellation, refusal before provider work at the spend ceiling,
the safe response shape, the upload size and type refusals (413 and 415), the Redis outage policy,
the developer-only usage view, and that a default-configured turn is not refused. It runs against
fakeredis with the database-dependent routes stubbed, so it runs without Docker.
