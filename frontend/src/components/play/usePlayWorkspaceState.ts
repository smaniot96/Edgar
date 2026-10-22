import { useEffect, useState } from "react";

import { isJournalView, viewFor, type JournalView } from "../journal/model";
import type { JournalCategory } from "../journal/types";

export type PlayTool = "journal" | "map" | "character" | null;

export interface PlayWorkspaceState {
  activeTool: PlayTool;
  query: string;
  view: JournalView;
  selectedEntryId: string | null;
}

function keyFor(sessionId: number): string {
  return `edgar_play_workspace:v1:${sessionId}`;
}

function readState(sessionId: number, initialTool: PlayTool): PlayWorkspaceState {
  try {
    const parsed = JSON.parse(localStorage.getItem(keyFor(sessionId)) ?? "null") as (Partial<PlayWorkspaceState> & { category?: string }) | null;
    const validTool = parsed?.activeTool === "journal" || parsed?.activeTool === "map" || parsed?.activeTool === "character" || parsed?.activeTool === null;
    if (parsed && validTool) {
      return {
        activeTool: parsed.activeTool ?? null,
        query: typeof parsed.query === "string" ? parsed.query : "",
        view: isJournalView(parsed.view) ? parsed.view :
          parsed.category && ["goal", "milestone", "clue", "hint", "note", "person", "place", "promise", "clock", "consequence"].includes(parsed.category)
            ? viewFor(parsed.category as JournalCategory) : "overview",
        selectedEntryId: typeof parsed.selectedEntryId === "string" ? parsed.selectedEntryId : null,
      };
    }
  } catch { /* storage unavailable or malformed */ }
  return { activeTool: initialTool, query: "", view: "overview", selectedEntryId: null };
}

/** Session-scoped tool/filter state survives navigation away from and back to the play route. */
export function usePlayWorkspaceState(sessionId: number, initialTool: PlayTool) {
  const [state, setState] = useState(() => readState(sessionId, initialTool));
  useEffect(() => {
    try { localStorage.setItem(keyFor(sessionId), JSON.stringify(state)); } catch { /* best effort */ }
  }, [sessionId, state]);
  return [state, setState] as const;
}
