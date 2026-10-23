# Experimental combat-grid backend

The backend can persist an encounter battlefield, resolve typed square moves and expose positions in the existing combat API and turn stream. It is **disabled by default**. The current frontend still presents the range-band encounter view; there is no clickable tactical grid.

Set `EDGAR_COMBAT_GRID=1` in the API process environment to opt newly opened encounters into this experiment. The default is `0`. Existing encounters without a `battlefield` keep their range-band rules. Once an encounter has a persisted battlefield, it keeps grid rules across reloads and configuration changes. Restarting with the flag off only changes future encounter creation. Run migrations before using the new application revision: migration `24_combat_battlefield` adds a nullable JSONB column and leaves existing rows null.

The engine generates a deterministic synthetic battlefield from the scene words and encounter roster. This is an encounter abstraction, not an authored source map. Every creature occupies one five-foot square. `battlefield` contains dimensions and terrain; each combatant carries its stored `pos` and `speed_ft`. Public projections expose `position: {x, y, label}`, `speed_ft` and `battlefield: {width, height, square_ft, theme, terrain}`. Default encounters omit these additive fields. The generated stream schema and TypeScript types describe both forms.

The implemented subset includes:

- Five-foot diagonals; walls block movement; difficult terrain and allies cost double movement. Creatures cannot end on another occupied square or pass through a hostile square.
- Typed moves such as `I move to D4`, moves toward a named foe and retreats. Multiple moves consume one shared route budget. A move before and after an attack is clarified instead of silently reordered; supported conditional moves use the remaining budget.
- Thirty-foot hero speed, recorded monster speed and Dash. Grappled, restrained, stunned and paralyzed creatures have zero movement. Routes are checked before confirmation/dice and replanned when the hero acts because earlier combatants may have moved.
- Weapon melee reach and normal/long ranged distances. Long range imposes disadvantage; an out-of-range attack is refused before dice. Maneuvers use five-foot reach even when the equipped weapon has reach.
- Enemy movement toward its chosen hero, melee Dash without an attack when normal movement cannot reach, and movement-triggered enemy opportunity attacks. Disengage applies in the player's declared order. Grid movements join the existing round-event history.
- Persisted terrain and creature positions, including insert/update and a fresh database-session reload. Bands remain a projection of squares for older combat consumers.

The experiment is not listed as a generally supported preparation capability. These limits require further work and live acceptance before default activation:

- No grid UI, square-selection controls or path preview; backend tests are not a live campaign acceptance run.
- No line of sight, cover, elevation, flanking, creature footprints, authored terrain or area templates. Walls currently block movement only; attacks and spells can pass through them.
- Spell/save-action range and area geometry are not enforced by this grid. Existing spell behavior continues; Ray of Frost's speed reduction is not implemented.
- Prone crawling/standing costs, armor and class speed changes, forced movement, grapple dragging/breaking on separation and complete fear-aware enemy routing are incomplete.
- Flight uses the legacy fallback movement model; burrowing, climbing and swimming are not modeled. Hero opportunity attacks remain limited to the existing Ready mechanism. Multiple melee reaches use the longest reach for enemy opportunity attacks.
- Terrain is deterministic scenery inferred from words, not a verified source interpretation. Some existing rule-boundary and monster declaration text still describes the default range-band engine. Party targeting and readied attacks retain their existing abstractions.

The opt-in boundary preserves useful backend work while these gaps remain explicit. The review receipt is in `docs/reviews/2026-10-23-combat-grid/REVIEW.md`.
