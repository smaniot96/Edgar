export type JournalCategory =
  | "goal"
  | "milestone"
  | "clue"
  | "hint"
  | "person"
  | "place"
  | "promise"
  | "clock"
  | "consequence";

export type JournalStatus =
  | "discovered"
  | "encountered"
  | "visited"
  | "active"
  | "completed"
  | "failed"
  | "fulfilled"
  | "broken"
  | "advanced"
  | "friendly"
  | "neutral"
  | "hostile"
  | "changed";

export interface JournalStatusChange {
  status: JournalStatus;
  turn_number: number | null;
  history_event_id: number | null;
}

export interface JournalOrigin {
  turn_number: number | null;
  history_event_id: number | null;
  place: string | null;
  actor: string | null;
  citation: string | null;
}

export interface JournalEntry {
  id: string;
  category: JournalCategory;
  title: string;
  text: string | null;
  status: JournalStatus;
  quantity: number | null;
  origin: JournalOrigin;
  history: JournalStatusChange[];
  related_entry_id: string | null;
}

export interface JournalRead {
  session_id: number;
  tracking: "available" | "legacy_untracked";
  entries: JournalEntry[];
  current_place: JournalEntry | null;
  active_goal: JournalEntry | null;
}
