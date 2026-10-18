# Session broadcast channel (EDG-047)

The live shared-session channel for multiplayer M2: every member of a campaign hears the acting
player's turn as it commits, sees who is at the table, and can resume after a dropped connection
without gaps or duplicates. Code: `apps/api/services/session_channel.py` (store, presence,
admission), `session_broadcast.py` (turn mapping), `session_stream.py` (subscriber loop),
`apps/api/routers/session_events.py` (HTTP), `apps/api/schemas/channel.py` (envelopes and the event
registry). Tests: `tests/api/test_session_channel.py` (store and loop, fakeredis) and
`tests/api/test_session_events_api.py` (routes, Postgres in token mode).

Input is unchanged: a member still submits a turn with `POST /api/sessions/{id}/turn` or
`/turn/stream`. The channel is server-to-client fan-out plus presence, and it is additive. Solo
play (`EDGAR_AUTH_MODE=local`) keeps the exact per-request turn stream frames.

## Transport: why SSE over GET, with POST for everything else

| Option | Verdict |
| --- | --- |
| **SSE (`GET /api/sessions/{id}/events`), read with `fetch`** | **Chosen.** Fan-out is one-way, which is what the table needs. The bearer token travels in the `Authorization` header, so it never appears in a URL, log line or browser history. `Last-Event-ID` is the standard resume cursor. It reuses the turn stream's framing, its `X-Accel-Buffering: no` header and the existing proxy path, and it needs no protocol upgrade. |
| WebSocket | Rejected for now. Browsers cannot set headers on the handshake, so the token would go in the URL, in a subprotocol, or in a first message that the server must hold open unauthenticated. It also needs proxy upgrade configuration, a separate origin check (cross-site WebSocket hijacking), and a bidirectional protocol we do not need: input already has a POST path. Revisit if we need low-latency client-to-server signalling. |
| `EventSource` | Rejected. It cannot send an `Authorization` header, so it would force the token into the query string. The client uses `fetch` streaming instead. |
| SSE with the token in a query parameter | Rejected outright: tokens in URLs leak into access logs and referrers. The server ignores `?access_token=` and returns 401. |
| Long polling | Rejected. Chatty, adds latency per event, and gives no ordering guarantee without the same sequencing work. |
| Redis pub/sub only | Rejected. Fire-and-forget: a dropped connection loses events, and there is no replay. Pub/sub would also be needed alongside a log. |
| Per-process in-memory fan-out | Rejected. It breaks as soon as there are two API workers. |

Fan-out and replay use **one Redis Stream per session**. Each subscriber reads the stream after its
cursor, so live delivery and replay are the same code path.

## Routes

| Method | Path | Who | Purpose |
| --- | --- | --- | --- |
| GET | `/api/sessions/{id}/events` | any member | SSE subscription. Resume with `Last-Event-ID: <seq>`. |
| GET | `/api/sessions/{id}/presence` | any member | Roster now: display name, seated character, `active`/`idle`. |
| POST | `/api/sessions/{id}/presence` | any member | Report own attention: `{"state": "active" \| "idle"}`. 204. |

A non-member gets the same 404 as a missing session on all three. Every access check goes through
the EDG-046 helpers (`member_session`, `campaign_role`). Authorization is re-resolved from the
database when a subscription starts, every `RECHECK_SECONDS` (5 s), and as soon as an `access.changed`
record is read. A membership change (`DELETE /members/{id}`, role change, `POST /leave`) commits and
then publishes `access.changed` to the campaign's sessions, so a removed member is cut off at once.

## Wire format

Each delivered record is one SSE frame:

```
id: 42
event: narration.committed
data: {"seq":42,"type":"narration.committed","channel":"narration","actor":{...},"turn_id":"...","at":"...","data":{...}}

```

- `id` is the session sequence number (`seq`). Clients send it back as `Last-Event-ID`.
- `event` is the event type (see the registry). Clients listen for the types they know and ignore the rest.
- `data` is the envelope. The audience is never sent to clients.
- `event: snapshot` (with `id` = head) is sent to a new subscriber, and to one whose cursor fell out of the retained window. It carries shared state, the roster, and (for a seated player only) the viewer's own character. It contains no narration history: re-read `GET /api/sessions/{id}/messages`.
- `event: closed` (no `id`) ends the stream, with `data.reason`:
  - `access_revoked`: do not reconnect; the next attempt returns 404.
  - `server_shutdown`, `lifetime`, `slow_consumer`: reconnect with the last `id` you saw.
- `: ping` comments every 15 s keep proxies and clients awake and refresh presence.
- `retry: 3000` tells a reconnecting client how long to wait.

Envelope fields: `seq` (int), `type`, `channel` (`table`, `narration`, `rolls`, `system`,
`private`), `actor` (`user_id`, `display_name`, `character_id`, `character_name`, or null for
system events), `turn_id`, `at` (UTC ISO time), `data` (type-specific).

## Event registry, channels and audiences

The registry in `apps/api/schemas/channel.py` is an allowlist. Publishing an unregistered type raises.

| Type | Channel | Audience | Carries |
| --- | --- | --- | --- |
| `turn.action` | table | table | The text the acting player submitted, as the turn starts. |
| `turn.stage` | system | table | Progress: parsing, retrieving, adjudicating, narrating, saving. |
| `narration.committed` | narration | table | Committed narration, public citations, rules edition. |
| `rolls.committed` | rolls | table | The public dice outcome. The private `rest` evidence is removed. |
| `state.committed` | system | table | Scene id, public combat roster, campaign-complete flag. |
| `turn.finished` | system | table | `outcome`: `committed`, `not_committed` or `unknown`. |
| `character.state` | private | **that player only** | The acting character's play state and the rest evidence. |
| `turn.failed` | private | **that player only** | The safe error for the player who submitted the turn. |
| `presence.joined` / `left` / `idle` / `active` | system | table | `user_id` and the presence state change. |
| `access.changed` | system | **control** | Internal wake-up. No client ever receives it. |

Audiences, and the one rule that decides delivery (`audience_visible`, used for live and replay alike):

- `table`: the owner, every player and every spectator.
- `host`: the campaign owner only. No registered event uses it yet. It is used for an event with no account behind it (single-user local mode).
- `player:<user_id>`: that one seated player, and nobody else. The owner does not receive another player's private data. A spectator never receives a player record, even one about themselves once demoted.
- `control`: never delivered.

**Never published, by construction:** streamed narration tokens (uncommitted text); the
`adjudication` frame (it can carry engine reasoning and story effects); the `debug` frame
(developer diagnostics); and the `rest` evidence on the table channel. Tests assert these reach no
subscriber.

## Ordering, delivery and idempotency

- **Ordering.** Each session has one integer sequence. A publish reads it and appends to the log in
  one optimistic transaction (`WATCH` on the sequence key), so the log order equals sequence order
  even with several workers publishing. Sequences are contiguous: every number has a record, and
  filtered records are skipped by the server rather than leaving holes the client must interpret.
- **At least once.** A reconnect can deliver a record again if the client never acknowledged it,
  and a snapshot can overlap records already applied. Clients must therefore:
  - ignore any frame whose `seq` is at or below the last one applied;
  - treat `narration.committed` as idempotent by `turn_id` (or `data.event_id`);
  - treat `state.committed` and `character.state` as replace, not merge;
  - on `turn.finished` with `outcome: unknown` (a disconnect mid-turn), re-read state rather than guess.
- **Turn lifecycle.** For each turn, the table sees `turn.action`, then `turn.stage` frames, then
  either `narration.committed`, `rolls.committed`, `state.committed`, `character.state` and
  `turn.finished(committed)`, or `turn.finished(not_committed|unknown)` (with `turn.failed` privately).
  The `done` is published after the transaction commits, so a member who re-reads state after
  `turn.finished` sees the committed turn.
- **Replay.** `Last-Event-ID: n` returns exactly the retained records with `seq > n` that the
  subscriber may see now, in order. A new subscriber (no `Last-Event-ID`) gets a snapshot and the
  live tail, not history. A cursor older than the retained window, or ahead of the log, gets a snapshot.

## Presence

- Presence is per connection. A member is present while at least one of their connections is live.
  `presence.joined` is announced when the first connection arrives and `presence.left` when the last
  one goes, whether by close, by TTL expiry, or by a sweep after a worker died.
- A connection is live for `PRESENCE_TTL_SECONDS` (30 s) and its heartbeat (every 15 s, the SSE
  loop) refreshes it. A connection whose heartbeat arrived after expiry is announced left, then
  joined again.
- Roster entries carry only `user_id`, `display_name`, the seated `character_id`/`character_name`
  and `state`. Nothing else, and never an email.
- `idle` is client-reported (`POST /presence`). It is announced only when it changes and only for a
  present member.
- Clocks: expiry compares wall-clock epoch seconds across workers, so workers must agree to within
  a few seconds (NTP is enough).

## Limits and timings

| Setting (`session_channel.py` / `session_stream.py`) | Value | Effect |
| --- | --- | --- |
| `RETENTION` | 500 records per session | Replay window. Older cursors get a snapshot. |
| `STREAM_TTL_SECONDS` | 30 days | Idle session logs expire. The sequence key never does, so sequences never restart. |
| `MAX_SUBSCRIBERS_PER_SESSION` | 32 | Live connections per session. Excess gets 429 `subscription_limit`. |
| `MAX_CONNECTIONS_PER_USER_SESSION` | 3 | One member's tabs on one session. Excess gets 429. |
| `MAX_STREAMS_PER_PROCESS` | 200 | Open streams per API process. Excess gets 429. |
| `SLOW_CONSUMER_SECONDS` | 10 s | A frame the client takes longer than this to accept closes the stream (`slow_consumer`). Memory per subscriber stays constant because nothing is queued in the process: the log is the queue. |
| `MAX_STREAM_SECONDS` | 1800 s | A stream ends (`lifetime`) and the client reconnects, bounding any single connection. |
| `HEARTBEAT_SECONDS` / `PRESENCE_TTL_SECONDS` | 15 s / 30 s | Ping cadence and presence expiry. |
| `RECHECK_SECONDS` | 5 s | Access re-check cadence. A control record forces an immediate check. |
| `READ_BLOCK_MS` | 1 s | Longest wait on an empty log before a presence and shutdown check. |
| publish timeout | 2 s | A publish that stalls is logged and abandoned. The turn still commits. |

Connection admission and the process counter are checked before the stream opens, so a refused
client gets a normal JSON 429 and no stream. The per-session and per-user checks read the live
connection set without a lock, so under a burst of simultaneous connects they can admit one or two
over the limit. That is accepted; the limits exist to bound resources, not to be exact.

Graceful shutdown: the app lifespan sets a drain flag. Every open stream ends with
`closed: server_shutdown` at its next tick (within about one second), and the client reconnects to
another worker.

## Database and connection use

No stream holds a pooled database connection. Token resolution commits its read transaction before
the route runs (`require_authentication`). The subscriber opens a short session for each access check,
identity read and snapshot, and closes it. Turn publishing reads the actor in its own short session.

## Redis requirements and verification

- **Version:** Redis **5.0 or later** (the commands used are XADD with explicit IDs and `MAXLEN`,
  XREAD with `COUNT` and `BLOCK`, XRANGE with `COUNT`, WATCH/MULTI, and sorted-set commands). Verified
  against Redis 6.2.24 and 7.4.11. The integration suite asserts the server version at start.
- **Real-Redis suite:** `tests/api/test_session_channel_real_redis.py` runs only when
  `EDGAR_TEST_REDIS_URL` is set, so CI is unchanged. It starts separate OS processes, each with its own
  client, and checks: 200 concurrent publishes give contiguous, unique sequences with stream order equal
  to sequence order and each publisher's own order kept; a live subscriber gets every event exactly once
  in order while publishers run; a resume after a cut mid-run replays exactly the missed visible records;
  MAXLEN trimming past the window forces a snapshot; and a killed worker's presence expires and is
  announced. Verified on 7.4.11 (four runs) and 6.2.24, with two publishers and with four and eight
  (the contention settings).

## Deployment requirements

- Redis is required (already a dependency for turn locks and rate limits). Multiple API workers are
  safe: all state is in Redis.
- The reverse proxy must not buffer `text/event-stream`. The API sends `X-Accel-Buffering: no`, and
  the proxy must honour it or disable response buffering for `/api/sessions/*/events`.
- The proxy's idle timeout must exceed 15 s (the heartbeat interval). The default of common proxies
  (60 s) is enough.
- Cross-origin hosts must be allowed `Last-Event-ID` (it is added to the allowed request headers).
  `Authorization` is already allowed, which is the only credential.
- No nginx or compose configuration in this repository proxies `/api`. Whoever operates the hosting
  setup must apply the two proxy rules above.

## Failure behaviour

- A Redis outage during a turn: the turn still commits or fails as before. Its channel records are
  skipped and logged by event name. Members see a stalled `turn.stage`; when Redis returns they
  re-read state.
- A Redis outage while subscribing: the subscription request fails with the usual server error and
  the client retries.
- A missed `access.changed` (Redis down at the moment of removal): the subscriber's periodic
  re-check (at most 5 s) still removes access, and a new subscription is refused by the database.

## Follow-ups (not in EDG-047)

- **Ephemeral narration deltas.** Tokens are not broadcast; members see the committed narration at
  the end of the turn. A live token channel (not stored, not replayed) needs a second Redis
  subscription per connection and is left for a later change.
- **Spotlight and turn-change events.** `turn_changed` and spotlight arbitration are EDG-048. The
  spotlight holder is in the snapshot but no event announces a change yet.
- **Client UI.** The lobby, roster and "whose turn" UI are EDG-050. The client protocol helper in
  `frontend/src/api/sessionChannel.ts` is the only frontend piece in this change.
- **Metrics.** Counters for open streams, replay size, slow-consumer closes and publish failures
  belong with the operational metrics (EDG-053 family), not here.
