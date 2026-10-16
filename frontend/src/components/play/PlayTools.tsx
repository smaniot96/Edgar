import { useRef, useState } from "react";

import { JournalPanel } from "../journal/JournalPanel";
import type { JournalCategory, JournalRead } from "../journal/types";
import type { SessionMapRead } from "../../api/types";
import { Button } from "../ui/Button";
import type { PlayTool } from "./usePlayWorkspaceState";
import { useModalFocus } from "./useModalFocus";
import { useNarrowViewport } from "./useNarrowViewport";
import { SessionMapPanel } from "./SessionMapPanel";

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
          className="min-h-11"
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
  sessionId,
  map,
  mapLoading,
  mapError,
  onMapRefresh,
  onOpenMapJournalEntry,
  onPrepareTravel,
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
  sessionId: number;
  map: SessionMapRead | null;
  mapLoading: boolean;
  mapError: unknown;
  onMapRefresh: () => void;
  onOpenMapJournalEntry: (entryId: string) => void;
  onPrepareTravel: (place: string) => void;
}) {
  const panelRef = useRef<HTMLElement | null>(null);
  const narrow = useNarrowViewport();
  const open = activeTool === "journal" || activeTool === "map";
  const [expandedJournal, setExpandedJournal] = useState(false);
  const [expandedMap, setExpandedMap] = useState(false);
  const [largeJournalText, setLargeJournalText] = useState(false);
  const modal = narrow || (activeTool === "map" && expandedMap);
  useModalFocus(open, modal, onClose, panelRef);
  if (!open) return null;

  const title = activeTool === "journal" ? "Journal" : "Map";
  return (
    <>
      {modal ? <button type="button" aria-label="Close adventure tool" className="fixed inset-0 z-30 cursor-default bg-black/55" onClick={onClose} /> : null}
      <aside
        ref={panelRef}
        aria-label={`${title} tool`}
        role={modal ? "dialog" : undefined}
        aria-modal={modal ? true : undefined}
        tabIndex={modal ? -1 : undefined}
        className={`fixed inset-y-0 right-0 z-40 flex ${activeTool === "map" && narrow ? "w-full" : "w-[min(28rem,92vw)]"} flex-col border-l border-line bg-canvas shadow-2xl shadow-black/60 ${activeTool === "map" && expandedMap ? "lg:inset-4 lg:w-auto" : `lg:static lg:z-auto lg:shrink-0 lg:shadow-none ${activeTool === "map" ? "lg:w-[min(60vw,60rem)]" : activeTool === "journal" && expandedJournal ? "lg:w-[min(55vw,52rem)]" : "lg:w-[min(30vw,25rem)]"}`}`}
      >
        <header className="flex shrink-0 items-center gap-3 border-b border-line px-4 py-3">
          <h2 className="min-w-0 flex-1 font-display text-lg font-semibold text-ink">{title}</h2>
          {activeTool === "map" && !narrow ? (
            <Button className="min-h-11" variant="secondary" size="sm" aria-expanded={expandedMap} onClick={() => setExpandedMap((value) => !value)}>
              {expandedMap ? "Side view" : "Full view"}
            </Button>
          ) : null}
          {activeTool === "journal" ? (
            <>
              <Button
                className="max-lg:hidden"
                variant="secondary"
                size="sm"
                aria-expanded={expandedJournal}
                onClick={() => setExpandedJournal((value) => !value)}
              >
                {expandedJournal ? "Compact view" : "Expand view"}
              </Button>
              <Button
                variant="secondary"
                size="sm"
                aria-pressed={largeJournalText}
                onClick={() => setLargeJournalText((value) => !value)}
              >
                {largeJournalText ? "Standard text" : "Larger text"}
              </Button>
            </>
          ) : null}
          <button
            type="button"
            onClick={onClose}
            aria-label={`Close ${title.toLowerCase()}`}
            className="min-h-11 min-w-11 rounded p-1 text-ink-muted hover:bg-surface-raised hover:text-ink focus-visible:outline focus-visible:outline-2 focus-visible:outline-ember-400"
          >
            ✕
          </button>
        </header>
        <div className={activeTool === "map" ? "flex min-h-0 flex-1 flex-col" : "min-h-0 flex-1 overflow-y-auto px-4 py-4 pb-[max(1rem,env(safe-area-inset-bottom))]"}>
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
              largeText={largeJournalText}
            />
          ) : (
            <SessionMapPanel
              sessionId={sessionId}
              actors={journal.data?.entries.filter((entry) => entry.category === "person")}
              map={map}
              loading={mapLoading}
              error={mapError}
              onRefresh={onMapRefresh}
              onOpenJournal={onOpenMapJournalEntry}
              onPrepareTravel={onPrepareTravel}
            />
          )}
        </div>
      </aside>
    </>
  );
}
