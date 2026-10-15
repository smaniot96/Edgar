import { useEffect, useState } from "react";

import type { JournalCategory } from "../journal/types";

export type PlayTool = "journal" | "map" | "character" | null;
export type JournalFilter = JournalCategory | "all";

export interface PlayWorkspaceState {
  activeTool: PlayTool;
  query: string;
  category: JournalFilter;
  selectedEntryId: string | null;
}

function keyFor(sessionId: number): string {
  return `edgar_play_workspace:v1:${sessionId}`;
}

function readState(sessionId: number, initialTool: PlayTool): PlayWorkspaceState {
  try {
    const parsed = JSON.parse(localStorage.getItem(keyFor(sessionId)) ?? "null") as Partial<PlayWorkspaceState> | null;
    const validTool = parsed?.activeTool === "journal" || parsed?.activeTool === "map" || parsed?.activeTool === "character" || parsed?.activeTool === null;
    if (parsed && validTool) {
      return {
        activeTool: parsed.activeTool ?? null,
        query: typeof parsed.query === "string" ? parsed.query : "",
        category: parsed.category === "all" || ["goal", "milestone", "clue", "hint", "person", "place", "promise", "clock", "consequence"].includes(parsed.category as string)
          ? parsed.category as JournalFilter
          : "all",
        selectedEntryId: typeof parsed.selectedEntryId === "string" ? parsed.selectedEntryId : null,
      };
    }
  } catch { /* storage unavailable or malformed */ }
  return { activeTool: initialTool, query: "", category: "all", selectedEntryId: null };
}

/** Session-scoped tool/filter state survives navigation away from and back to the play route. */
export function usePlayWorkspaceState(sessionId: number, initialTool: PlayTool) {
  const [state, setState] = useState(() => readState(sessionId, initialTool));
  useEffect(() => {
    try { localStorage.setItem(keyFor(sessionId), JSON.stringify(state)); } catch { /* best effort */ }
  }, [sessionId, state]);
  return [state, setState] as const;
}
