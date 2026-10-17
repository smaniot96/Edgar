# Operational metrics (EDG-139)

Aggregate, bounded-cardinality operational counters and histograms for the Edgar API, exported in the
Prometheus text format from `GET /metrics`. Status: implemented and unit-tested. **Nothing is deployed,
scraped or alerting yet.** The example rules in `ops/alerts.example.yml` are untuned placeholders.

## Decision

| Option | Decision | Why |
| --- | --- | --- |
| `prometheus_client` | Not used | Not in `uv.lock`; adding it is a new dependency for ~200 lines of exposition. |
| OpenTelemetry SDK + collector | Not used now | Needs a collector service or an exporter choice; the brief forbids new infrastructure. Revisit when a backend is chosen. |
| Persisted DevPanel rows only | Insufficient | Per-turn rows answer "what happened in this turn", not "turns per minute and p95 over a week". |
| **In-process registry, hand-written exposition** | **Chosen** | No dependency, no new service, bounded by construction, trivially resettable in tests. |

Consequences: counters live in each API process. The Dockerfile runs a single uvicorn worker, so one
process holds all the counters. If the API is ever run with several workers, scrape each one or
aggregate in the scraper; the counters are not shared across processes.

## What is measured and where it comes from

Committed DM turns are recorded from the `turn_diagnostics` row that was persisted with the turn
(`observe_committed_turn`, called right after persistence in `turn_runner.py` and `routers/session.py`).
Turns that end without a commit are recorded from their partial collector (`observe_uncommitted_turn`, in
the router's `finally`), so provider failures during a failed turn are still counted. A turn is counted
once, even if both paths see it.

| Series | Type | Labels (closed sets) | Source |
| --- | --- | --- | --- |
| `edgar_turns_total` | counter | `path` normal/combat, `delivery` sync/stream, `outcome` committed/failed | every DM turn that reached the runner |
| `edgar_turn_duration_seconds` | histogram | `path`, `delivery` | committed row `total_ms` |
| `edgar_turn_stage_duration_seconds` | histogram | `stage` (the shared stage buckets) | committed row `stages_ms`; unmeasured stages are not observed |
| `edgar_turn_time_to_first_text_seconds` | histogram | `delivery` | committed row, streamed turns that produced text |
| `edgar_provider_calls_total` | counter | `kind` (parse, adjudicate, narrate, combat_setup, summarize, other), `result` ok/error | provider call records |
| `edgar_provider_call_duration_seconds` | histogram | `kind` | provider call `latency_ms` |
| `edgar_provider_failures_total` | counter | `kind`, `category` (the closed `ERROR_CATEGORIES`) | provider call `error_category` |
| `edgar_provider_retries_total` | counter | `kind` | caller-level `attempts` beyond the first (SDK-internal retries are not visible) |
| `edgar_provider_tokens_total` | counter | `kind`, `type` input/output/total | reported usage; only measured values are added |
| `edgar_provider_usage_unknown_calls_total` | counter | `kind` | calls without a reported input or output count |
| `edgar_provider_estimated_cost_usd_total` | counter | `kind` | ESTIMATE at configured conservative rates; only models with a configured price |
| `edgar_provider_cost_unknown_calls_total` | counter | `kind` | calls with no configured price, so no estimate |
| `edgar_retrieval_total` | counter | `result` ok/degraded | turn retrieval `degraded` flag |
| `edgar_retrieval_duration_seconds` | histogram | none | retrieval `latency_ms` |
| `edgar_turn_lock_acquisitions_total` | counter | `result` acquired/contended | `acquire_turn_lock`; `contended` is every 409 from the lock |
| `edgar_turn_lock_wait_seconds` | histogram | none | the Redis acquire round trip, every attempt |
| `edgar_readiness_probes_total` | counter | `result` ready/unavailable | `GET /ready` as a whole |
| `edgar_readiness_checks_total` | counter | `component` postgres/schema/redis/qdrant, `result` | each readiness check |
| `edgar_readiness_check_duration_seconds` | histogram | `component` | each readiness check (health-probe latency, a separate family from turn latency) |

Rules that the code enforces:

- **Labels are closed.** Any value outside its vocabulary is recorded as `other`, so a stray value can never
  create a series. Session ids, user ids, campaign ids, turn ids, prompts, model output, provider messages and
  free-text errors are never labels and are not accepted as arguments.
- **Unknown stays unknown.** A missing token count, cost or latency is not recorded as zero. Token and cost
  counters grow only by measured values; calls with unknown usage or cost are counted in their own series, and
  if nothing was measured the series is absent rather than zero.
- **Recording is best effort.** Every `observe_*` function swallows its own errors and logs a fixed event name
  (`operational_metrics_record_failed`), never the exception text. A metrics fault cannot fail a turn, a
  readiness probe or a lock acquisition. Recording performs no I/O and holds no database connection.
- **Latency histograms describe committed turns.** A failed turn contributes to `edgar_turns_total`
  (outcome `failed`), its provider calls and retrieval, but not to the duration or stage histograms.

## Exposure

`GET /metrics` is not public by default.

- `EDGAR_METRICS_ENABLED` must be `1`, `true` or `yes`. Otherwise the endpoint answers **404**, the same
  shape as the developer-only diagnostics routes gated by `EDGAR_DEBUG`. The counters are still recorded
  in-process; only the exposure is gated.
- `EDGAR_METRICS_TOKEN` (optional). When set, the scraper must send `Authorization: Bearer <token>`; a missing
  or wrong token gets **403**. The token is never logged or echoed. Store it as a secret, not in the repo.

Compose: the API is published only on `127.0.0.1:8000`, so the endpoint is reachable from the host alone. A
Prometheus container on the same Compose network should scrape the service by name without publishing the
port. This is a sketch, not a checked-in deployment:

```yaml
# prometheus.yml (sketch; not deployed)
rule_files:
  - /etc/prometheus/alerts.example.yml   # copy of ops/alerts.example.yml
scrape_configs:
  - job_name: edgar-api
    metrics_path: /metrics
    scrape_interval: 30s
    authorization:
      type: Bearer
      credentials_file: /run/secrets/edgar_metrics_token   # only if EDGAR_METRICS_TOKEN is set
    static_configs:
      - targets: ["api:8000"]
```

The API environment for that scrape needs `EDGAR_METRICS_ENABLED=1` (and `EDGAR_METRICS_TOKEN` if used).

## Health probes and turn latency are separate

`/health` is not instrumented. `/ready` records each component check in
`edgar_readiness_check_duration_seconds` and the overall result in `edgar_readiness_probes_total`. Turn latency
is only in `edgar_turn_duration_seconds` and the stage/first-text histograms. A dashboard that charts turn p95
should not include readiness probes, and it does not, because the families are distinct.

## Example alert rules (untuned)

`ops/alerts.example.yml` contains rules for: scrape down, provider failure rate, p95 committed-turn latency,
turn-lock contention, readiness failures and degraded retrieval. **Every threshold is a placeholder** and must
be tuned against real traffic before anyone relies on it. Nothing routes these alerts to a person.

## Known gaps

- Counters reset when the API process restarts. Use the scraper's `rate`/`increase` windows, not raw totals.
- A request rejected before a turn starts (for example a 404 or a readiness gate) is not a turn and is not counted.
- A stream whose client disconnects after the stream has started and before the turn commits is counted as
  `failed`. A disconnect after commit is counted as committed. A client that disconnects before the stream body
  starts is not counted.
- `edgar_provider_retries_total` counts only the caller's own attempts. SDK-internal retries are not visible.
- Cost is an estimate at configured conservative rates. It is not a billed amount. Only models with a configured
  price (currently `gpt-6.1-sol`) produce a cost; every other model's calls are counted in
  `edgar_provider_cost_unknown_calls_total`.
- Log-based alerting and a restore drill (the other parts of DB-I5) are not covered by this change.

## Verification

- `tests/api/test_operational_metrics.py` (no Docker): registry behaviour and thread safety, valid exposition with
  a label allow-list, closed vocabularies, unknown usage never zero-filled, once-per-turn recording, failures that
  never reach a turn, lock contention, readiness observations, and the 404/403/200 endpoint gate.
- The sync router hook and the Postgres-backed paths are exercised only by hosted CI, which needs Docker.
