# Responsive play verification (EDG-040)

The play shell follows the unzoomed visual viewport height and top offset, allowing a
keyboard resize to keep the composer within the visible area. At pinch zoom it falls back
to CSS dynamic viewport units. Orientation, source recovery, recap and ideas share a bounded
scroll region; the tools toolbar and composer remain reachable outside that region. Source
blockers are shown once and still disable sending. Long title disclosure is keyboard/touch
accessible, and the character sheet exposes the full hero name.

The composer grows with its draft, caps its height relative to the visible viewport, and
scrolls longer input. Enter, Shift+Enter, IME composition, uncertain-turn and busy guards
are preserved. Closing tools and crossing the 1024px breakpoint retains the session draft,
returns focus and removes modal inert state. Common play/close/read controls target 44px.

## Recorded synthetic matrix — 16 October 2026

Google Chrome 155.0.8059.82, Playwright 1.57, reduced motion, local Vite UI, synthetic
campaign/hero/history/journal/inventory/map fixtures. External requests were blocked and
API writes rejected. No provider, paid generation or real user data was used.

| Viewport / condition | Result |
| --- | --- |
| 320×568, 390×844, 1280×720; healthy and missing-source | No measured horizontal overflow; composer within viewport; true blocker retained |
| 390×844 with 200% root text | Natural nav reflow, readable text, composer visible; tool strip scrolls horizontally |
| 390×360 short viewport | Composer and tools remain reachable; orientation scrolls |
| Journal/map/character tools; desktop ↔ narrow breakpoint | Draft retained, close reachable, inert state removed after close |
| 150+ character/unbroken campaign title, non-Latin hero, long narrative/clue/inventory | Full title and hero accessible; content wraps; long details scroll |

Root review found and corrected an offscreen title popover, 320px/enlarged-text navigation
overflow, a native PDF input wider than its recovery panel, duplicate blocker space, and
an unbounded orientation stack. The jump-to-latest button is clipped to its story region
so a tall draft cannot put that floating control over the tools toolbar.

These results are desktop viewport emulation and automated DOM interaction. They do not
establish physical-phone soft-keyboard, rotate, safe-area, real-touch or assistive-technology
acceptance. EDG-040 remains awaiting those explicitly required UX-R03/R04/R06 observations.
