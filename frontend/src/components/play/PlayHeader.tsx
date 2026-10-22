import { Link } from "react-router-dom";

import { cn } from "../../lib/cn";
import type { SheetCharacter } from "./gameState";
import { HpBar } from "../ui/HpBar";

export function PlayHeader({
  title,
  campaignId,
  character,
  sheetOpen,
  onToggleSheet,
  isDev,
  sessionId,
  sceneId,
  tableHref,
}: {
  title: string;
  campaignId: number | null;
  character: SheetCharacter | null;
  sheetOpen: boolean;
  onToggleSheet: () => void;
  isDev: boolean;
  sessionId: number;
  sceneId: string | null;
  /** Link to the multiplayer lobby for this session (EDG-050). */
  tableHref?: string;
}) {
  const subtitle = character
    ? [character.className, character.level != null ? `Lv ${character.level}` : null]
        .filter(Boolean)
        .join(" · ")
    : null;
  return (
    <header className="flex shrink-0 flex-wrap items-center gap-2 border-b border-line bg-canvas/90 px-3 py-2 sm:flex-nowrap sm:gap-3 sm:px-4">
      <div className="min-w-0 w-full flex-1 sm:w-auto">
        <div className="flex min-w-0 items-start gap-1.5">
          <h1 className="min-w-0 flex-1">
            {campaignId != null ? (
              <Link
                to={`/campaigns/${campaignId}`}
                className="line-clamp-2 lg:line-clamp-1 whitespace-normal break-words [overflow-wrap:anywhere] font-display text-lg font-semibold leading-tight text-ink hover:text-ember-200"
              >
                {title}
              </Link>
            ) : (
              <span className="line-clamp-2 lg:line-clamp-1 whitespace-normal break-words [overflow-wrap:anywhere] font-display text-lg font-semibold leading-tight text-ink">{title}</span>
            )}
          </h1>
          <details className="group relative z-20 shrink-0">
            <summary className="flex min-h-11 min-w-11 cursor-pointer list-none items-center justify-center rounded-md px-2 text-[0.7rem] font-medium text-ember-300 underline decoration-ember-400 underline-offset-2 hover:bg-surface-raised focus-visible:outline focus-visible:outline-2 focus-visible:outline-ember-400 [&::-webkit-details-marker]:hidden">
              Full title
            </summary>
            <div className="absolute right-0 top-full z-50 mt-1 max-h-[min(35dvh,14rem)] w-[min(24rem,calc(100vw-1.5rem))] overflow-y-auto rounded-lg border border-line-strong bg-canvas p-3 text-sm text-ink shadow-xl shadow-black/50 whitespace-normal break-words [overflow-wrap:anywhere]">
              {title}
            </div>
          </details>
        </div>
        {isDev ? (
          <div className="mt-1 flex max-w-full flex-wrap gap-x-2 gap-y-0.5 break-words font-mono text-[0.65rem] text-ember-400/80 [overflow-wrap:anywhere]">
            <span>session {sessionId}</span>
            <span>scene {sceneId ?? "—"}</span>
            {character?.id != null ? <span>char {character.id}</span> : null}
          </div>
        ) : null}
      </div>

      {tableHref ? (
        <Link
          to={tableHref}
          className="inline-flex min-h-11 shrink-0 items-center rounded-control px-2 text-sm font-medium text-ember-300 underline decoration-ember-400 underline-offset-2 hover:text-ember-200 focus-visible:outline focus-visible:outline-2 focus-visible:outline-ember-400"
        >
          Table
        </Link>
      ) : null}

      {character ? (
        <button
          type="button"
          onClick={onToggleSheet}
          aria-expanded={sheetOpen}
          aria-controls="character-sheet"
          aria-label={`${sheetOpen ? "Hide" : "Show"} character sheet for ${character.name || "Adventurer"}`}
          className={cn(
            "flex min-h-11 w-full min-w-0 shrink-0 flex-col gap-0.5 rounded-lg border px-2.5 py-1.5 text-left transition-colors sm:w-44 sm:py-1 sm:px-2.5 md:w-56 lg:gap-0 lg:py-1",
            "focus-visible:outline focus-visible:outline-2 focus-visible:outline-ember-400",
            sheetOpen
              ? "border-ember-700/70 bg-ember-950/30"
              : "border-line bg-surface/70 hover:border-line-strong",
          )}
        >
          <span className="flex min-w-0 flex-col gap-0.5 text-sm">
            <span className="line-clamp-1 min-w-0 whitespace-normal break-words font-semibold text-ink [overflow-wrap:anywhere]">{character.name || "Adventurer"}</span>
            {subtitle ? <span className="truncate text-xs text-ink-muted">{subtitle}</span> : null}
          </span>
          <HpBar current={character.hpCurrent} max={character.hpMax} />
        </button>
      ) : null}
    </header>
  );
}
