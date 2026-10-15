import { useRef } from "react";

import { JournalPanel } from "../journal/JournalPanel";
import type { JournalCategory, JournalRead } from "../journal/types";
import { Button } from "../ui/Button";
import type { PlayTool } from "./usePlayWorkspaceState";
import { useModalFocus } from "./useModalFocus";
import { useNarrowViewport } from "./useNarrowViewport";

export function PlayToolBar({
  activeTool,
  onToggle,
  disabled,
}: {
  activeTool: PlayTool;
  onToggle: (tool: Exclude<PlayTool, null>) => void;
  disabled?: boolean;
}) {
  const tools: { id: Exclude<PlayTool, null>; label: string }[] = [
    { id: "journal", label: "Journal" },
    { id: "map", label: "Map" },
    { id: "character", label: "Character" },
  ];
  return (
    <nav aria-label="Adventure tools" className="flex shrink-0 gap-2 overflow-x-auto border-b border-line bg-canvas/80 px-3 py-2 sm:px-4">
      {tools.map((tool) => (
        <Button
          key={tool.id}
          type="button"
          variant={activeTool === tool.id ? "primary" : "secondary"}
          size="sm"
          disabled={disabled}
          aria-pressed={activeTool === tool.id}
          onClick={() => onToggle(tool.id)}
        >
          {tool.label}
        </Button>
      ))}
    </nav>
  );
}

export function PlayToolPanel({
  activeTool,
  journal,
  query,
  onQueryChange,
  category,
  onCategoryChange,
  selectedEntryId,
  onSelectedEntryChange,
  onRefresh,
  onClose,
  onOpenHistory,
}: {
  activeTool: PlayTool;
  journal: { data: JournalRead | null; loading: boolean; error: unknown; stale: boolean };
  query: string;
  onQueryChange: (value: string) => void;
  category: JournalCategory | "all";
  onCategoryChange: (value: JournalCategory | "all") => void;
  selectedEntryId: string | null;
  onSelectedEntryChange: (id: string | null) => void;
  onRefresh: () => void;
  onClose: () => void;
  onOpenHistory: (eventId: number) => void;
}) {
  const panelRef = useRef<HTMLElement | null>(null);
  const narrow = useNarrowViewport();
  const open = activeTool === "journal" || activeTool === "map";
  useModalFocus(open, narrow, onClose, panelRef);
  if (!open) return null;

  const title = activeTool === "journal" ? "Journal" : "Map";
  return (
    <>
      {narrow ? <button type="button" aria-label="Close adventure tool" className="fixed inset-0 z-30 cursor-default bg-black/55" onClick={onClose} /> : null}
      <aside
        ref={panelRef}
        aria-label={`${title} tool`}
        role={narrow ? "dialog" : undefined}
        aria-modal={narrow ? true : undefined}
        tabIndex={narrow ? -1 : undefined}
        className="fixed inset-y-0 right-0 z-40 flex w-[min(28rem,92vw)] flex-col border-l border-line bg-canvas shadow-2xl shadow-black/60 lg:static lg:z-auto lg:w-[min(30vw,25rem)] lg:shrink-0 lg:shadow-none"
      >
        <header className="flex shrink-0 items-center gap-3 border-b border-line px-4 py-3">
          <h2 className="min-w-0 flex-1 font-display text-lg font-semibold text-ink">{title}</h2>
          <button
            type="button"
            onClick={onClose}
            aria-label={`Close ${title.toLowerCase()}`}
            className="rounded p-1 text-ink-muted hover:bg-surface-raised hover:text-ink focus-visible:outline focus-visible:outline-2 focus-visible:outline-ember-400"
          >
            ✕
          </button>
        </header>
        <div className="min-h-0 flex-1 overflow-y-auto px-4 py-4 pb-[max(1rem,env(safe-area-inset-bottom))]">
          {activeTool === "journal" ? (
            <JournalPanel
              {...journal}
              onRefresh={onRefresh}
              selectedEntryId={selectedEntryId}
              onSelectedEntryChange={onSelectedEntryChange}
              category={category}
              onCategoryChange={onCategoryChange}
              query={query}
              onQueryChange={onQueryChange}
              onOpenHistory={onOpenHistory}
            />
          ) : (
            <section aria-labelledby="map-unavailable-heading" className="rounded-control border border-line bg-surface p-4">
              <h3 id="map-unavailable-heading" className="font-serif text-lg font-semibold text-ink">No map is available</h3>
              <p className="mt-2 text-sm leading-relaxed text-ink-muted">
                This session does not have a map to show yet. The journal still tracks the places the story has recorded.
              </p>
            </section>
          )}
        </div>
      </aside>
    </>
  );
}
