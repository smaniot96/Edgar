# API

FastAPI application: routers (`campaigns`, `sessions`, `characters`, `seed`, `health`), schemas, services, and the React chat UI served under `/ui` when `frontend/dist` is built. The agent is imported in-process from `apps/agent`; there is no separate agent service.

## Chat UI

See [`frontend/README.md`](../../frontend/README.md). Build with `make frontend-build` (or `cd frontend && npm run build`); the Docker API image runs `npm run build` and copies `frontend/dist` into the container. Open `http://localhost:8000/ui/` after `make up`. The UI calls `POST /api/seed` from the home screen for a default user, campaign, character, and session, then streams turns via SSE on `/play/:sessionId`.

## Turn endpoints

Two endpoints share the same lock and persistence path; they differ in transport.

`POST /api/sessions/{session_id}/turn`. One JSON response after the agent finishes. Use this for `curl`, scripts, and tests. Errors are standard HTTP: `404` if the session is missing, `409` if another turn holds the Redis lock, `503` if the agent returns an `error` field, `500` on uncaught exceptions.

`POST /api/sessions/{session_id}/turn/stream`. Server-Sent Events. Used by the UI. Frame types are `status` (stages: `parsing`, `retrieving`, `adjudicating`, `narrating`, `saving`), `token` (narration chunks; concatenated they form the full narration), `adjudication` (the structured `AdjudicationResult` once), `done` (`{narration, character, current_scene_id}`), and `error` (`{detail}`). Lock contention (409) is returned as a normal JSON error before the stream opens; failures after streaming begins arrive as an `error` event and the connection then closes.

```
curl -N -X POST "http://localhost:8000/api/sessions/1/turn/stream" \
  -H 'content-type: application/json' \
  -d '{"message":"I look around"}'
```

## Turn lifecycle

`session.py::_load_turn_initial_state` reads the session, campaign, character, prior narration messages (last 10 turns), world flags, and any active combat state into the agent's `initial_state`. `acquire_turn_lock` then sets a Redis key `turn:{session_id}` with `NX EX 60`. The agent runs (`app.ainvoke`); `apply_adjudication` writes HP, conditions, inventory, world flags, and scene transitions in the same DB transaction; an `event_log` row of `event_type="narration"` is appended; the lock is released in `finally`.

`current_user_id()` looks up the seeded `dm@edgar.local` user; this is the single-user seam for solo play and is replaced by real auth when there is more than one user.

## Health

`/health` returns 200 without touching the DB (cheap; for compose healthchecks). `/ready` returns 200 only when `SELECT 1` succeeds, 503 otherwise (for orchestrators that need a real readiness signal).

## Schemas

`schemas/` mirrors the SQLAlchemy ORM. `Create`/`Update` are request bodies; `Read` is the response body with `from_attributes=True` so ORM rows serialize directly.
