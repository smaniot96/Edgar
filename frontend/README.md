# Edgar frontend

React 19 + TypeScript (strict) + Vite + Tailwind CSS 3 + React Router 7. Served in production from the API at `/ui/*` (built files live in `frontend/dist/` and are copied into the API Docker image).

## Layout

- `src/main.tsx` — router: `/` landing splash; inside the app shell `/campaigns`, `/campaigns/:id`, `/characters`, `/settings`, `/play/:sessionId`; `*` → 404. Route errors render a themed error page (`routes/RouteError.tsx`).
- `src/App.tsx` — app shell: nav + a fixed-height (`h-dvh`) column. Pages own their scrolling (`components/ui/Page`), which lets Play pin its composer.
- `src/routes/` — pages. `Home` is the Campaigns page; `Campaign` is the campaign hub with tabs in `routes/campaign/`; `Play` is the chat UI.
- `src/components/ui/` — design-system primitives: `Button`/`ButtonLink`, `Field`/`TextField`/`Input`/`Textarea`/`Select`, `Tabs`/`TabPanel`, `Page`/`PageHeader`, `EmptyState`, `ErrorNotice`/`OfflineState`, icons, shared class strings (`styles.ts`).
- `src/components/` — `CharacterForm`, `Pill`, `EdgarLogo`, and the chat components.
- `src/api/` — `client.ts` (typed `fetch` helpers; `ApiError.message` is player-friendly, raw details via `errorDetails`), SSE parsing, response types.
- `src/lib/` — `useQuery` (loading/error/data/refetch with abort), `cn`, campaign display names, date formatting, last-session pointer.
- `src/dev/` — User/Developer mode (`ModeProvider`, `useMode`, nav toggle) and the per-turn `DevPanel`. Developer mode reveals ids, raw errors and debug data.

## Design tokens

Defined in `tailwind.config.ts` — use these instead of hex values:

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
2. From repo root: `make frontend-install` once, then `make frontend-dev` (Vite on **http://localhost:5173/ui/** with HMR). After editing `tailwind.config.ts`, restart Vite if new tokens don't show up.
3. Vite proxies `/api` to `http://localhost:8000` (`vite.config.ts`). Leave `VITE_API_BASE_URL` unset so the browser calls same-origin `/api` and the proxy applies.
4. Before committing: `npm run lint` and `npx tsc -b`.

Production-style check: `make frontend-build`, then run the API — `GET /ui` serves the bundle from `frontend/dist/`.

## Docker

The root `Dockerfile` builds this package in a `frontend-build` stage and copies `dist/` into `/app/frontend/dist` for the `api` image. `Dockerfile.frontend` documents a standalone multi-stage build if you need only the static assets.
