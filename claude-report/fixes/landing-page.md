# Landing page + Edgar logo

Date: 2026-06-18

Dark-fantasy hero landing page as the new entry point at `/`, plus a custom inline-SVG
EDGAR wordmark (dragon over a volcano "A" with smoke). Work confined to `frontend/`.

## Files

- **`frontend/src/components/EdgarLogo.tsx`** (new) — self-contained, scalable inline-SVG
  wordmark. Reads "EDGAR" where the **A is a volcano** (triangular silhouette, molten
  crossbar band, glowing lava crater + rising smoke plume); a **dragon** with spread wings
  arcs above the word; **smoke wisps curl around the E**. Palette: ember/orange + molten-red
  glow, ash-grey smoke, pale-gold letterforms on `#101216`, via SVG radial/linear gradients.
  `variant` prop: `"hero"` (landing) and `"mark"` (compact nav). Gradient ids are suffixed per
  variant to stay unique when both render. Accessible (`role="img"`, `aria-label`, `<title>`).
- **`frontend/src/routes/Landing.tsx`** (new) — full-bleed splash rendered *outside* the app
  nav chrome. Centered hero logo, a one-sentence pitch, dark vignette + molten radial glow,
  a thin molten divider, faint rising ember particles (pre-computed positions, no per-render
  randomness), and three CTA cards: primary "Enter the Campaigns" → `/campaigns`, plus
  "Your Characters" → `/characters` and "Settings" → `/settings`. Mobile-responsive
  (`grid sm:grid-cols-3`, logo scales via `max-w`).
- **`frontend/src/main.tsx`** — routing change: `/` is now a top-level `<Landing />` route
  (no nav). The `<App />` nav layout keeps the rest; Campaigns (`Home`) is now the canonical
  `path: "campaigns"` (removed the old `index` alias).
- **`frontend/src/App.tsx`** — replaced the plain "Edgar" nav text with the compact
  `EdgarLogo variant="mark"` wrapped in a `Link` to `/` (home splash); "Campaigns" NavLink now
  points to `/campaigns`.
- **`frontend/src/routes/Campaign.tsx`** — updated the 3 "← Campaigns" / post-delete
  navigations from `/` to `/campaigns` so they land on the campaigns list, not the splash.
- **`frontend/src/routes/Home.tsx`** — updated only the header doc-comment (no longer the index).
- **`frontend/src/index.css`** — added GPU-friendly keyframes (`edgar-smoke-drift`,
  `edgar-ember-flicker`, `edgar-ember-rise`) and the `.edgar-ember-dot` particle style. All
  motion is disabled under `prefers-reduced-motion`; transform/opacity only, no layout jank.

## Why

The app previously dropped users straight onto the Campaigns screen. A dedicated splash
explains Edgar in one sentence, presents a striking branded logo, and provides clear tabbed
entry points into the app, while keeping the existing nav-wrapped routes intact.

## Constraints honored

No new dependencies (pure React + Tailwind + inline SVG + a little CSS). TypeScript strict-clean.
package.json / lockfile untouched.

## Verify

`npm run build` (`tsc -b && vite build`) **succeeded** — no type/build errors; the rolldown
darwin binding was already present. `npm run lint` reports 0 issues in the new/edited files;
the 3 remaining lint errors are pre-existing (`Home.tsx` set-state-in-effect, `NewAdventure.tsx`
unused disable) in code untouched by this change.
