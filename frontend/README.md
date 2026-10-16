# Edgar frontend

React 19 + TypeScript (strict) + Vite + Tailwind CSS 4 + React Router 7. Served in production from the API at `/ui/*` (built files live in `frontend/dist/` and are copied into the API Docker image).

The Tailwind 4 browser baseline is Chrome 111+, Safari 16.4+, and Firefox 128+.

## Layout

- `src/main.tsx` — router: `/` landing splash; inside the app shell `/campaigns`, `/campaigns/:id`, `/characters`, `/settings`, `/play/:sessionId`; `*` → 404. Route errors render a themed error page (`routes/RouteError.tsx`).
- `src/App.tsx` — app shell: nav + a fixed-height (`h-dvh`) column. Pages own their scrolling (`components/ui/Page`), which lets Play pin its composer.
- `src/routes/` — pages. `Home` coordinates the Campaigns page, with creation/profile forms in `routes/home/`; `Campaign` is the campaign hub with tabs in `routes/campaign/`; `Play` is the chat UI.
- `src/components/ui/` — shared primitives: `Button`/`ButtonLink`, `Field`/`TextField`/`Input`, `Tabs`/`TabPanel`, `Page`/`PageHeader`, `EmptyState`, `ErrorNotice`/`OfflineState`, `HpBar`, icons, shared class strings (`styles.ts`).
- `src/components/` — `CharacterForm`, `Pill`, `EdgarLogo`, campaign cards, and the chat components. Campaign and play screens use the same HP meter.
- `src/api/` — `client.ts` owns REST requests and response decoding (`ApiError.message` is player-friendly, raw details via `errorDetails`); `sse.ts` parses event framing, and `turnStream.ts` handles turn events/errors. Streaming request cancellation and the watchdog stay in `components/play/useTurn.ts`.
- `src/lib/` — `useQuery` (loading/error/data/refetch with abort), `cn`, campaign display names/card data, date formatting, last-session pointer.
- `src/dev/` — User/Developer mode (`ModeProvider`, `useMode`, nav toggle) and the per-turn `DevPanel`. Developer mode reveals ids, raw errors and debug data.

## Design tokens

Defined in the `@theme` block in `src/index.css` — use these instead of hex values:

| Token | Use |
| --- | --- |
| `canvas` | page background |
| `surface`, `surface-sunken`, `surface-raised` | cards, inputs/insets, hovers |
| `line`, `line-strong` | borders |
| `ink`, `ink-muted`, `ink-subtle` | text (all ≥ 4.5:1 on `canvas`) |
| `ember-{50…950}` (DEFAULT 500) | amber accent: primary actions, links, focus rings |
| `danger`, `success`, `info`, `warning` (+ `-soft`) | status colours |
| `font-display` + `text-display-{sm,md,lg}` | serif headings (Cormorant Garamond → Georgia) |
| `rounded-control`, `rounded-card`, `shadow-ember` | radii and glow |

## Development

1. Start the API stack (`make up`, or `uv run uvicorn apps.api.main:app` with Postgres/Redis as in compose).
2. From repo root: `make frontend-install` once, then `make frontend-dev` (Vite on **http://localhost:5173/ui/** with HMR). After editing the `@theme` block in `src/index.css`, restart Vite if new tokens don't show up.
3. Vite proxies `/api` to `http://localhost:8000` (`vite.config.ts`). Leave `VITE_API_BASE_URL` unset so the browser calls same-origin `/api` and the proxy applies.
4. Before committing: `npm run lint`, `npm test`, and `npm run build` (includes strict TypeScript and unused-local/parameter checks).

The dev proxy does not buffer or time out turn streams. Measured on macOS with Node 24: a throwaway SSE server on port 8000 and `vite` on 5173, one token frame per second from 5 s to 95 s, then `done` at 96 s. Per-frame latency from server write to client arrival was at most 1 ms both direct and through `/api`. A 60 s and a 120 s idle gap passed through unchanged, the proxy added no `Content-Encoding` to an `Accept-Encoding: gzip` request, and a client abort closed the upstream connection within about 10 ms. The proxy therefore needs no timeout or buffering options. The app's own 90 s turn watchdog (`TURN_WATCHDOG_MS` in `src/components/play/useTurn.ts`) is independent of the proxy and still stops a turn after 90 s without frames.

Production-style check: `make frontend-build`, then run the API — `GET /ui` serves the bundle from `frontend/dist/`.

## Docker

The root `Dockerfile` builds this package in a `frontend-build` stage and copies `dist/` into `/app/frontend/dist` for the `api` image. `Dockerfile.frontend` documents a standalone multi-stage build if you need only the static assets.
