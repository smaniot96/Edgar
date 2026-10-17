# Operational logging: aggregation, access, retention and incident lookup (EDG-063)

Status: the repository side is implemented and statically tested. **Nothing is deployed.** No log sink
exists, no shipper has run, no alert threshold is tuned, and no restore or access drill has been performed
on a real host. Metrics are covered separately in [operational-metrics.md](operational-metrics.md)
(EDG-139) and the metric alert examples in [../ops/alerts.example.yml](../ops/alerts.example.yml).

## What the logs contain today

- One structured JSON line per event, from the API (`apps/api/logging_config.py`), the agent and the
  services it imports. In `ENV=local` the console renderer is used instead.
- Correlation: every HTTP request has an `X-Request-ID` (a bounded proxy value is kept, anything else is
  replaced by a UUID). It is returned on the response and bound into every line for the request, including
  streamed and background work.
- Failure lines carry the safe taxonomy from `log_failure()` (`apps/api/services/safe_errors.py`):
  `category`, `support_ref` (`err-` plus 16 hex characters, also shown to the user), `outcome`
  (`committed`, `not_committed`, `unknown`), `retryable`, and where known `session_id`, `campaign_id`,
  `turn_id`. Provider and validation messages are not logged: `error_summary()` in
  `core/src/edgar_core/log_safety.py` keeps the exception class, HTTP status and provider request id only.
- Docker keeps each container's stdout in a `json-file` log rotated at 3 files x 10 MB
  (`docker-compose.yml`, `x-logging`). That bounds disk use. It is not storage, search, or recovery.

### Field contract

| Class | Fields | Rule |
| --- | --- | --- |
| Correlation | `request_id`, `support_ref`, `turn_id`, `run_id`, `session_id`, `campaign_id`, `slug` | Allowed. Internal identifiers, not prose. Treat `session_id` as linking to player data. |
| Outcome | `event`, `level`, `category`, `outcome`, `retryable`, `error_class`, `failure_code`, `stage` | Allowed. Fixed vocabularies. |
| Counts and timing | `chunks`, `bytes`, `size`, `attempt`, `*_ms`, `*_tokens`, `turns` | Allowed. |
| Forbidden prose and payloads | `narration`, `message`, `messages`, `prompt`, `prompts`, `player_input`, `player_text`, `content`, `completion`, `body`, `response_body`, `raw_response` | Never logged by call sites. Redacted by the processor if added anyway. |
| Forbidden credentials | `authorization`, `cookie`, `set_cookie`, `password`, `api_key`, `openai_api_key`, `x_api_key`, `secret`, `client_secret`, `token`, `access_token`, `refresh_token` | Never logged. Redacted by key name at any depth. |

The full list is `FORBIDDEN_LOG_FIELDS` in `apps/api/logging_config.py`. Three layers enforce it:

1. **Call sites.** `tests/api/test_log_contract.py` scans every production log call (AST) for these keyword
   and `extra=` keys and fails on any match. It is self-checked on synthetic code so it cannot pass vacuously.
2. **Processor.** `redact_sensitive_data` replaces forbidden keys with `[REDACTED]` at any depth, and
   redacts credential patterns (URL credentials, Bearer tokens, `sk-` keys, PostgreSQL `DETAIL`) in string
   values and tracebacks. Tests: `tests/api/test_log_field_redaction.py`, `tests/api/test_logging.py`.
3. **Edge.** `ops/logging/vector.toml` removes the same keys again before shipping, and drops every
   non-JSON line. A test fails if its list drifts from `FORBIDDEN_LOG_FIELDS`.

Known limits of this design, all open:

- Pattern redaction catches known credential forms only. Free text in a value under an allowed key is not
  detectable. The call-site rule is the real control.
- `exc_info=True` tracebacks keep exception messages (for example the turn-lock renewal and memory summary
  failures). Only `DETAIL` and credential patterns are redacted there.
- The access log records the request path and query string. Keep private data out of query strings
  (repository rule: never place personal data in URLs).
- The ingestion CLI configures its own Loguru sink and does not use this processor. Its
  `Upsert failed for batch {}: {}` line in `apps/ingestion/src/ingestion/embed.py` interpolates the raw
  exception text. It is not shipped today, but it must not be shipped without a fix.
- `adventure_upload_accepted` logs the uploaded `filename`. A file name can be private. It is not prose, so
  it is kept, but review it before any log is shared beyond the operators.

Findings fixed in this change: `narration_roster_violation` logged the first two narration words for an
invented attacker; it now logs roster names or a fixed label. `readiness_check_failed` logged `repr(exc)`;
it now logs the error class. The startup rejection of invalid provider settings now emits
`provider_configuration_invalid` before the process exits, so there is a structured alert signal.

## Aggregation choice

| Option | Decision | Why |
| --- | --- | --- |
| Keep Docker `json-file` only | Insufficient | Rotation loses history, there is no search across containers, and nothing survives a host loss. |
| Docker `docker_logs` source or a Docker logging plugin | Not used | Needs the Docker API socket, which is root-equivalent on the host. Tailing the log files read-only needs no socket. |
| Vendor SaaS agent | Not chosen | No vendor is selected, and the data is private campaign content. Choose later under the organisation's data-processing terms. |
| Fluent Bit | Not chosen | Equally capable and lighter. Its filtering is Lua or a chain of filters, which is harder to test statically than one VRL program. |
| **Vector, file source over Docker logs** | **Chosen as the example** | Read-only tailing, typed parsing that fails closed (`parse_json!`), a readable in-pipeline removal list, a disk buffer, and an HTTP sink that any protected store can accept. |

The example is vendor-neutral. Its sink is a placeholder HTTP endpoint from environment variables, so no
endpoint or token is committed. The LogQL alert examples in [../ops/alerts.logs.example.yml](../ops/alerts.logs.example.yml)
assume a Loki-compatible backend with a `job` label. Adapt them if the chosen store differs.

Scope of the shipper: it reads the log files of every container on the host, because Docker's file names
are container ids and carry no container name. Non-JSON lines from PostgreSQL, Redis and Qdrant are
dropped at the edge, so their errors are not shipped. Keep those services' own diagnostics on the host.

## Access and protected storage

Who may read:

- Operators on call for the Edgar host, with named accounts and multi-factor sign-in on the log store.
- No read access for the application, the shipper, or general developer accounts. Developers see sanitised
  examples in tickets, not production lines.
- Read access to the archive tier is a separate, narrower role, granted per incident.

Protected storage:

- The sink lives in an administrative boundary separate from the Edgar host and its Docker volumes, as
  [recovery-drill.md](recovery-drill.md) requires for backups.
- Encryption at rest with an organisation-managed key. Encryption in transit: the sink URI must be `https`.
- The shipper holds a write-only credential, supplied from the host environment at start-up. It is never
  written into `.env` committed to the repository, and it cannot read what it wrote.
- No public endpoint, no anonymous write, no open bucket policy. Block public access on any object store.
- The Vector disk buffer (1 GiB in the example) holds unsent lines on the host. It is encrypted only if the
  host volume is. Use an encrypted host volume for `vector_data`.
- Log-store access and deletion events are themselves logged, and those audit records are kept for 12 months.

## Retention

Proposed periods. They are recommendations for the operator to confirm, not measured or legal requirements.

| Class | Where it lives | Retention | Notes |
| --- | --- | --- | --- |
| Operational logs (structured API/agent lines) | Log store, searchable | 30 days, then deleted | Enough for support_ref investigations, which are usually raised within days. Short retention limits exposure. |
| Operational log archive (optional tier) | Encrypted object store, restricted role | 90 days total, then deleted | Only if the store cannot search 30 days cheaply. Lifecycle rule deletes; no manual copies. |
| Shipper disk buffer | Host `vector_data` volume | Until delivered; capped at 1 GiB | A transport buffer, not a retention copy. |
| Docker json-file logs | Host | 3 x 10 MB per container | Rotation, not retention. Shipping must keep up with about 30 MB per container. |
| Alert history | Alert backend | Backend default, at most 30 days | Alerts carry no prose. |
| Log-store audit records | Log store or audit account | 12 months | Covers reads and deletions of operational logs. |
| Player history: narration, adjudications, memory summaries, rulings | PostgreSQL `event_log` (durable class in `db/postgres/models/event_logs.py`) | Campaign lifetime, per [recovery-drill.md](recovery-drill.md) | **Never shipped to the log store.** Logs must not contain it (see the field contract). |
| Diagnostic events (`combat_round`, `turn_diagnostics`, `turn_trace`) | PostgreSQL | Their own expiry policy, set by the product owner | Separate from operational logs. Not set by this change. |
| Database and source backups | Backup storage | Per [recovery-drill.md](recovery-drill.md) | Container log rotation is not a database retention policy. |

## Incident runbook: find a request by support_ref

A support reference looks like `err-` followed by 16 lowercase hexadecimal characters. It appears in the
error payload the user sees. Each failure has its own reference. `request_id` ties together every line for
one HTTP request, and it is returned in the `X-Request-ID` response header.

1. Check the format. Anything else is not a support reference. Do not search for prose the user pasted.
2. Search the log store for the reference, for example `{job="edgar-api"} | json | support_ref="err-..."`.
   The result gives `event`, `category`, `outcome`, `retryable`, and the correlation ids.
3. Pull the whole request: `{job="edgar-api"} | json | request_id="..."`. Read the lines in order, including
   the access line for the path and status.
4. Interpret the category. `provider_unavailable`, `rate_limited` and `quota_exceeded` point at the model
   provider. `provider_configuration` points at settings, and the startup alert fires if the API cannot start.
   `source` and `review` point at the campaign source. `assignment` and `busy` are user state.
   `internal` means a defect: open a ticket with the event name, support_ref and time, never with prose.
5. Check `outcome`. `committed` means the turn was saved and the player may see it. `not_committed` means it
   was not saved. `unknown` needs the database row check described in
   [recovery-drill.md](recovery-drill.md) before any statement to the user.
6. If nothing is found, check the retention window and the shipper's health. For a host with no store, the
   local `docker compose logs api` covers only the last rotated files, about 30 MB per container.
7. Record only the ids and the category in the incident note. Do not paste lines that contain free text.

## Not done (explicit)

- Nothing is deployed. The shipper, the sink, and the override have not been started, and no image has been
  pulled.
- The Vector program and the LogQL rules are not executed by any test here. CI validates structure only.
  Run `vector validate --no-environment ops/logging/vector.toml` on the operator's machine before first use,
  and test the alert queries against a real backend.
- The image tag `timberio/vector:0.46.1-alpine` is pinned but was not checked against a registry. Confirm it.
- No alert threshold is tuned. Each is a placeholder.
- No SIEM, no detection content, no cross-system correlation, no log-based paging policy.
- No restore or access drill for the log store.
- The migration failure rule is inactive until `migrate` emits a structured `migration_failed` event. A failed
  `make migrate` exit status is the only signal today.
- Ingestion CLI logs are not covered by the processor (see the findings above).

## Pre-hosting checklist

- [ ] Choose the store and confirm its data-processing terms, region and access model.
- [ ] Create the sink in the protected boundary, with encryption at rest, https-only ingestion, no public
      access, and 30-day expiry (lifecycle rule, not a manual job).
- [ ] Create the write-only shipper credential. Supply `EDGAR_LOG_SINK_URL` and `EDGAR_LOG_SINK_TOKEN` from the
      host environment or the secrets store. Never commit them.
- [ ] Encrypt the host volume that holds `vector_data`.
- [ ] Run `vector validate --no-environment ops/logging/vector.toml` and `docker compose -f docker-compose.yml
      -f docker-compose.logging.yml config` on the host. Confirm the shipper has no ports.
- [ ] Start the shipper with the profile. Confirm lines arrive, that non-Edgar containers produce no lines in
      the store, and that a forbidden test key is removed. Use a dummy log line; never a real prompt.
- [ ] Configure the `job="edgar-api"` label in the shipper or store, then load `ops/alerts.logs.example.yml`
      and test each query against real data. Tune every threshold from a baseline.
- [ ] Confirm `EDGAR_METRICS_ENABLED` and the metric alerts from EDG-139 are loaded alongside the log alerts.
- [ ] Walk through the support_ref runbook once with a synthetic failure on a disposable host.
- [ ] Confirm the operator role list, the 12-month audit retention, and who receives the alerts.
- [ ] Record the first measured retention and storage figures in this document.
