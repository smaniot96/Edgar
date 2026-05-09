# Edgar API

## Turn endpoints

Two ways to run a player turn for a session:

### `POST /api/sessions/{session_id}/turn`

- **Use case:** scripts, `curl`, automated tests, clients that want a single JSON response.
- **Errors:** Standard HTTP status codes (e.g. `409` if another turn holds the lock, `503` if the agent returns an error field, `500` on unexpected failure). The body is JSON or plain text depending on the handler.

### `POST /api/sessions/{session_id}/turn/stream`

- **Use case:** the web UI and any client that wants progressive updates.
- **Response:** `text/event-stream` (Server-Sent Events). Each message is a frame with `event:` and `data:` (JSON).
- **Event types:** `status` (stages: `parsing`, `retrieving`, `adjudicating`, `narrating`, `saving`), `token` (narration chunks), `adjudication` (structured result once), `done` (final `{ narration, character, current_scene_id }`), `error` (`{ detail }`).
- **Errors:** `409` for lock contention is returned **before** the stream starts (normal JSON error response). Failures that occur **after** the stream has begun are sent as an `error` event inside the SSE stream; the client should still read until the connection closes.

Example:

```bash
curl -N -X POST "http://localhost:8000/api/sessions/1/turn/stream" \
  -H 'content-type: application/json' \
  -d '{"message":"I look around"}'
```
