import { useEffect, useState } from "react";
import { previewCombatRoute } from "../../api/client";
import type { CombatRoutePreview, CombatState } from "../../api/types";

function column(index: number): string {
  let result = "";
  for (let value = index + 1; value > 0; value = Math.floor((value - 1) / 26)) {
    result = String.fromCharCode(65 + (value - 1) % 26) + result;
  }
  return result;
}

/** Inspection and route previews are reads. Only the separate draft button proposes a move. */
export function TacticalGrid({ combat, sessionId, onPrepareMove, onInspect }: {
  combat: CombatState;
  sessionId?: number;
  onPrepareMove?: (text: string) => void;
  onInspect: (id: string) => void;
}) {
  const field = combat.battlefield!;
  const [destination, setDestination] = useState<string | null>(null);
  const [dash, setDash] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [response, setResponse] = useState<{
    combat: CombatState; destination: string; dash: boolean; sessionId: number;
    attempt: number; value?: CombatRoutePreview; failed?: boolean;
  } | null>(null);
  const current = response?.combat === combat && response.destination === destination
    && response.dash === dash && response.sessionId === sessionId && response.attempt === attempt
    ? response : null;
  const preview = current?.value;
  const error = current?.failed;
  useEffect(() => {
    if (!destination || !sessionId || combat.ended) return;
    const controller = new AbortController();
    let active = true;
    previewCombatRoute(sessionId, destination, dash, controller.signal).then((value) => {
      if (active) setResponse({ combat, destination, dash, sessionId, attempt, value });
    }).catch(() => {
      if (active) setResponse({ combat, destination, dash, sessionId, attempt, failed: true });
    });
    return () => { active = false; controller.abort(); };
  }, [destination, dash, sessionId, combat, attempt]);

  // Defensive bounds keep a malformed snapshot from creating an unbounded UI.
  if (field.width < 1 || field.height < 1 || field.width > 64 || field.height > 64) {
    return <p role="status">The battlefield could not be displayed.</p>;
  }
  const path = new Set(preview?.path.map((square) => square.label));
  const choose = (square: string) => {
    setDestination(square); setAttempt((value) => value + 1);
  };
  return <section aria-label="Tactical battlefield" className="space-y-3">
    <p className="text-xs text-ink-muted">Each square is {field.square_ft} feet. Select a creature to inspect it, or an empty square to preview a move. Shaded squares are difficult terrain; solid blocks are walls.</p>
    <p className="text-xs text-ink-muted">Terrain is generated for this encounter and may differ from the adventure’s map.</p>
    <div className="max-w-full overflow-x-auto rounded border border-line">
      <div role="group" aria-label="Battlefield squares" className="grid gap-px bg-line p-px" style={{ gridTemplateColumns: `repeat(${field.width}, minmax(2.75rem, 1fr))`, minWidth: `${field.width * 2.75}rem` }}>
        {Array.from({ length: field.width * field.height }, (_, index) => {
          const x = index % field.width, y = Math.floor(index / field.width);
          const square = `${column(x)}${y + 1}`;
          const terrain = field.terrain.find((tile) => tile.x === x && tile.y === y);
          const actors = combat.combatants.filter((actor) => actor.position?.x === x && actor.position?.y === y);
          const actor = actors[0];
          const wall = terrain?.kind === "wall";
          return <button key={square} type="button" aria-label={`${square}${actors.map((row) => `, ${row.display_name || row.name}`).join("")}${terrain ? `, ${terrain.feature}` : ""}`} aria-pressed={destination === square} disabled={wall}
            onClick={() => {
              if (actor) onInspect(actor.id || actor.name);
              else choose(square);
            }}
            className={`min-h-14 min-w-11 overflow-hidden p-1 text-center text-xs focus-visible:z-10 focus-visible:outline focus-visible:outline-2 focus-visible:outline-ember-400 ${wall ? "bg-ink-muted text-canvas" : path.has(square) ? "bg-ember-950 text-ember-200" : terrain?.kind === "difficult" ? "bg-surface-raised text-ink" : "bg-canvas text-ink"} ${destination === square ? "ring-2 ring-inset ring-ember-400" : ""}`}>
            <span className="block text-[0.6rem]">{square}</span>
            <span className={`block truncate font-semibold ${actor?.is_player ? "text-ember-300" : ""}`}>{actor ? actor.display_name || actor.name : wall ? "■" : terrain ? "░" : "·"}</span>
          </button>;
        })}
      </div>
    </div>
    {destination ? <div aria-label="Movement preview" className="space-y-2 rounded border border-line bg-surface p-3">
      <p className="font-semibold">Move to {destination}</p>
      <label className="flex items-center gap-2 text-xs"><input type="checkbox" checked={dash} disabled={combat.ended} onChange={(event) => setDash(event.target.checked)} />Use Dash (spends your action)</label>
      <div role="status" className="text-xs text-ink-muted">
        {combat.ended ? "This encounter has ended." : error ? "The route could not be checked. Select a square to try again." : !sessionId ? "Route previews are unavailable." : !preview ? "Checking the route…" : <>
          <p>{preview.cost_ft != null ? `${preview.cost_ft} feet of ${preview.budget_ft} feet available.` : preview.reason}</p>
          {preview.cost_ft != null && preview.reason ? <p>{preview.reason}</p> : null}
          {preview.path.length > 1 ? <p>Path: {preview.path.map((square) => square.label).join(" → ")}</p> : null}
          {preview.provokes.length ? <p className="text-danger">Leaving reach can provoke: {preview.provokes.join(", ")}. Disengage requires a separate declaration.</p> : null}
        </>}
      </div>
      <button type="button" disabled={!onPrepareMove || !preview?.reachable || combat.ended} onClick={() => onPrepareMove?.(`I ${dash ? "Dash" : "move"} to ${destination}.`)} className="min-h-10 rounded border border-ember-500 px-3 py-1 text-sm text-ember-300 disabled:opacity-50">Add move to draft</button>
      <p className="text-xs text-ink-muted">Previewing spends no turn. The engine checks the move again when your hero acts.</p>
    </div> : null}
  </section>;
}
