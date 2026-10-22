export type JournalCategory =
  | "goal"
  | "milestone"
  | "clue"
  | "hint"
  | "person"
  | "place"
  | "promise"
  | "clock"
  | "consequence"
  | "note";

export type JournalStatus =
  | "discovered"
  | "known"
  | "announced"
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
  | "changed"
  | "captive"
  | "released"
  | "dead"
  | "defeated"
  | "learned";

export type JournalOwnership = "carried" | "left_behind" | "with_another_character" | "consumed" | "untracked";

export interface JournalStatusChange {
  status: JournalStatus;
  turn_number: number | null;
  history_event_id: number | null;
}

export interface JournalOrigin {
  turn_number: number | null;
  history_event_id: number | null;
  place: string | null;
  place_id?: string | null;
  actor: string | null;
  actor_id?: string | null;
  citation: string | null;
}

export interface JournalEntry {
  id: string;
  actor_id?: string | null;
  actor_aliases?: string[];
  actor_reference_event_ids?: number[];
  related_actor_id?: string | null;
  category: JournalCategory;
  title: string;
  text: string | null;
  status: JournalStatus;
  quantity: number | null;
  clock_maximum?: number | null;
  ownership?: JournalOwnership | null;
  origin: JournalOrigin;
  history: JournalStatusChange[];
  related_entry_id: string | null;
  related_entry_ids?: string[];
}

export interface JournalRead {
  session_id: number;
  tracking: "available" | "legacy_untracked";
  entries: JournalEntry[];
  current_place: JournalEntry | null;
  active_goal: JournalEntry | null;
  current_lead?: JournalEntry | null;
}
