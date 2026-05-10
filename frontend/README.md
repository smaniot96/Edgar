# Edgar frontend

React 18 + TypeScript (strict) + Vite + Tailwind v3 + React Router v6. Served in production from the API at `/ui/*` (built files live in `frontend/dist/` and are copied into the API Docker image).

## Layout

- `src/routes/` — page components (`Play` is the chat UI; other routes are placeholders until later plans).
- `src/api/` — `fetch` helpers, SSE parsing, shared response types.
- `src/hooks/` — `useApi` re-exports and `useSSE` turn-stream bridge.
- `src/components/` — chat primitives (`MessageList`, `ComposerBar`, `Pill`).

## Development

1. Start the API stack (or `uv run uvicorn apps.api.main:app` with Postgres/Redis as in compose).
2. From repo root: `make frontend-install` once, then `make frontend-dev` (Vite on **http://localhost:5173** with HMR).
3. Vite proxies `/api` to `http://localhost:8000` (`vite.config.ts`). Leave `VITE_API_BASE_URL` unset so the browser calls same-origin `/api` and the proxy applies.

Production-style check: `make frontend-build`, then run the API — `GET /ui` serves the bundle from `frontend/dist/`.

## Docker

The root `Dockerfile` builds this package in a `frontend-build` stage and copies `dist/` into `/app/frontend/dist` for the `api` image. `Dockerfile.frontend` documents a standalone multi-stage build if you need only the static assets.
