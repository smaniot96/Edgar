# AI adventure world map — v1.1
Input: the accepted premise, every place entity of the adventure (shared and chapter places), the chapters with their primary locations and leads, a summary of each chapter's events, the campaign secrets, size and preferences. Output: one AiWorldMap: a simple regional sketch map the players will see, like the hand-drawn map at the front of a published adventure.

Place every listed place exactly once on a 100 x 100 grid (x grows east, y grows south). Lay the region out so the story's travel makes sense: places visited together are near each other, the antagonist's site or stronghold sits apart or behind an obstacle, and the journey between chapters crosses the map. Keep places at least 8 grid units apart; a building inside a settlement sits just beside it. Choose a symbol for each place and a short player-safe label (no secrets in labels).

public marks what the heroes would know when play begins: their starting location, the main settlement and well-known landmarks. Hidden lairs, secret rooms, sealed vaults and places revealed by secrets are not public; they appear on the player's map only once the heroes reach or learn of them.

Connect every place to the rest of the map with routes: roads, paths, rivers, sea crossings, tunnels or bridges, each with a short label. Mark a route hidden when it is a secret way the heroes must discover. Add 3-10 terrain features that give the region character (coastline, bay, river course, forest, hills, marsh, fields, ruins), drawn as areas (3+ points) or lines (rivers, cliffs), with evocative names. Terrain is public scenery; never put a secret in a feature name.

Keep stable place IDs and distinguish regional approaches, exteriors and interiors where chapter geography makes them separate destinations. A map node is a place, not a repeated narrative phase or generic "unknown place" placeholder. Use one readable label for each actual place and connect routes according to authored access, including credible return/withdrawal paths; a route does not imply the hero already traveled it. Public labels/descriptions never expose unseen interior occupants, hidden identity or conditional actor staging. Every map claim must agree with the chapter's keyed running geography.

When revision is supplied, return the complete corrected map that fixes every listed problem and keeps sound placements.
