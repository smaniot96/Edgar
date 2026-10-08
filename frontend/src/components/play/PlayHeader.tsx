import { Link } from "react-router-dom";

import { cn } from "../../lib/cn";
import type { SheetCharacter } from "./gameState";
import { HpBar } from "./HpBar";

export function PlayHeader({
  title,
  campaignId,
  character,
  sheetOpen,
  onToggleSheet,
  isDev,
  sessionId,
  sceneId,
}: {
  title: string;
  campaignId: number | null;
  character: SheetCharacter | null;
  sheetOpen: boolean;
  onToggleSheet: () => void;
  isDev: boolean;
  sessionId: number;
  sceneId: string | null;
}) {
  const subtitle = character
    ? [character.className, character.level != null ? `Lv ${character.level}` : null]
        .filter(Boolean)
        .join(" · ")
    : null;
  return (
    <header className="flex shrink-0 items-center gap-3 border-b border-line bg-canvas/90 px-3 py-2 sm:px-4">
      <div className="min-w-0 flex-1">
        {campaignId != null ? (
          <Link
            to={`/campaigns/${campaignId}`}
            className="block truncate font-display text-lg font-semibold leading-tight text-ink hover:text-ember-200"
            title={`Back to ${title}`}
          >
            {title}
          </Link>
        ) : (
          <span className="block truncate font-display text-lg font-semibold leading-tight text-ink">{title}</span>
        )}
        {isDev ? (
          <div className="flex gap-2 font-mono text-[0.65rem] text-ember-400/80">
            <span>session {sessionId}</span>
            <span>scene {sceneId ?? "—"}</span>
            {character?.id != null ? <span>char {character.id}</span> : null}
          </div>
        ) : null}
      </div>

      {character ? (
        <button
          type="button"
          onClick={onToggleSheet}
          aria-expanded={sheetOpen}
          aria-controls="character-sheet"
          className={cn(
            "flex w-44 shrink-0 flex-col gap-0.5 rounded-lg border px-2.5 py-1 text-left transition-colors sm:w-56",
            "focus-visible:outline focus-visible:outline-2 focus-visible:outline-ember-400",
            sheetOpen
              ? "border-ember-700/70 bg-ember-950/30"
              : "border-line bg-surface/70 hover:border-line-strong",
          )}
          title={sheetOpen ? "Hide character sheet" : "Show character sheet"}
        >
          <span className="flex items-baseline gap-1.5 truncate text-sm">
            <span className="truncate font-semibold text-ink">{character.name || "Adventurer"}</span>
            {subtitle ? <span className="truncate text-xs text-ink-muted">{subtitle}</span> : null}
          </span>
          <HpBar current={character.hpCurrent} max={character.hpMax} />
        </button>
      ) : null}
    </header>
  );
}
