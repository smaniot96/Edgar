import type { JournalCategory, JournalEntry, JournalRead, JournalStatus } from "./types";

/** The journal's chapters: how a player thinks about what they know, not the ledger's kinds. */
export type JournalView = "overview" | "people" | "places" | "clues" | "goals" | "log";

export const entryDomId = (id: string) => `journal-entry-${id}`;

export const JOURNAL_VIEWS: readonly JournalView[] = ["overview", "people", "places", "clues", "goals", "log"];

export const VIEW_LABELS: Record<JournalView, string> = {
  overview: "Overview",
  people: "People",
  places: "Places",
  clues: "Clues",
  goals: "Goals",
  log: "Log",
};

const VIEW_OF: Record<JournalCategory, JournalView> = {
  person: "people",
  place: "places",
  clue: "clues",
  hint: "clues",
  note: "clues",
  goal: "goals",
  milestone: "goals",
  promise: "goals",
  clock: "goals",
  consequence: "log",
};

export function isJournalView(value: unknown): value is JournalView {
  return typeof value === "string" && (JOURNAL_VIEWS as readonly string[]).includes(value);
}

export function viewFor(category: JournalCategory): JournalView {
  return VIEW_OF[category];
}

export const CATEGORY_LABELS: Record<JournalCategory, string> = {
  goal: "Goal",
  milestone: "Milestone",
  clue: "Clue",
  hint: "Hint",
  person: "Person",
  place: "Place",
  promise: "Promise",
  clock: "Story urgency",
  consequence: "Story change",
  note: "Learned",
};

export function statusLabel(status: JournalStatus): string {
  switch (status) {
    case "announced": return "announced for later";
    case "known": return "heard of, not visited";
    case "learned": return "learned";
    default: return status.replaceAll("_", " ");
  }
}

const OPEN_STATUSES = new Set<JournalStatus>(["active", "announced", "advanced"]);
const DONE_STATUSES = new Set<JournalStatus>(["completed", "fulfilled"]);
const LOST_STATUSES = new Set<JournalStatus>(["failed", "broken", "dead", "defeated"]);

export function statusTone(status: JournalStatus): "success" | "danger" | "ember" | "info" | "neutral" {
  if (DONE_STATUSES.has(status)) return "success";
  if (LOST_STATUSES.has(status) || status === "hostile") return "danger";
  if (OPEN_STATUSES.has(status)) return "ember";
  if (status === "friendly" || status === "visited") return "info";
  return "neutral";
}

/** Read-only lookups over one safe projection; links to merged or unknown entries resolve to nothing. */
export interface JournalIndex {
  byId: Map<string, JournalEntry>;
  people: JournalEntry[];
  places: JournalEntry[];
  clues: JournalEntry[];
  notes: JournalEntry[];
  goals: JournalEntry[];
  /** Earned answers that point at an entry (a person they concern, a clue or place they belong to). */
  notesAbout: (entryId: string) => JournalEntry[];
  /** Notes not already shown inside a clue card. */
  looseNotes: JournalEntry[];
  clueForNote: (entryId: string) => JournalEntry | undefined;
  promisesTo: (personId: string) => JournalEntry[];
  peopleMetAt: (placeId: string) => JournalEntry[];
  /** Open threads: what the player can still act on. */
  threads: JournalEntry[];
  lead: JournalEntry | null;
  chronological: JournalEntry[];
}

export function lastRecordedTurn(entry: JournalEntry): number {
  return Math.max(entry.origin.turn_number ?? -1, ...entry.history.map((change) => change.turn_number ?? -1));
}

const byTurnDesc = (left: JournalEntry, right: JournalEntry) => lastRecordedTurn(right) - lastRecordedTurn(left);

/** The API chooses the latest earned lead; old responses use their existing safe order. */
export function currentThread(data: JournalRead | null): JournalEntry | null {
  if (data?.active_goal) return data.active_goal;
  if (data?.current_lead !== undefined) return data.current_lead;
  return data?.entries.find((entry) => ["clue", "hint", "note"].includes(entry.category)) ?? null;
}

export function indexJournal(data: JournalRead | null): JournalIndex {
  const entries = data?.entries ?? [];
  const byId = new Map(entries.map((entry) => [entry.id, entry]));
  const of = (...categories: JournalCategory[]) => entries.filter((entry) => categories.includes(entry.category));
  const notes = of("note");
  const clues = of("clue", "hint");
  const clueIds = new Set(clues.map((entry) => entry.id));
  const notesFor = new Map<string, JournalEntry[]>();
  for (const note of notes) {
    for (const id of note.related_entry_ids ?? []) {
      notesFor.set(id, [...(notesFor.get(id) ?? []), note]);
    }
  }
  const promises = of("promise");
  const people = of("person");
  const places = of("place").sort((left, right) => {
    // Where you are first, then visited places, then places only heard of.
    const rank = (entry: JournalEntry) => (entry.id === data?.current_place?.id ? 0 : entry.status === "visited" ? 1 : 2);
    return rank(left) - rank(right) || byTurnDesc(left, right);
  });
  const goals = of("goal", "milestone", "promise", "clock");
  const threads = [
    ...goals.filter((entry) => OPEN_STATUSES.has(entry.status)),
    ...places.filter((entry) => entry.status === "known"),
  ];
  const lead = data?.active_goal ? null : currentThread(data);
  return {
    byId,
    people,
    places,
    clues,
    notes,
    goals,
    notesAbout: (entryId) => notesFor.get(entryId) ?? [],
    looseNotes: notes.filter((note) => !(note.related_entry_ids ?? []).some((id) => clueIds.has(id))),
    clueForNote: (entryId) => (byId.get(entryId)?.related_entry_ids ?? [])
      .map((id) => byId.get(id)).find((entry) => entry?.category === "clue" || entry?.category === "hint"),
    promisesTo: (personId) => promises.filter((entry) => entry.related_actor_id === personId || entry.related_entry_id === personId),
    peopleMetAt: (placeId) => people.filter((entry) => entry.origin.place_id === placeId),
    threads,
    lead,
    chronological: [...entries].sort(byTurnDesc),
  };
}

export function entriesForView(index: JournalIndex, view: JournalView): JournalEntry[] {
  switch (view) {
    case "people": return index.people;
    case "places": return index.places;
    case "clues": return [...index.clues, ...index.looseNotes].sort(byTurnDesc);
    case "goals": return index.goals;
    case "log": return index.chronological;
    case "overview": return [];
  }
}

export function matchesQuery(entry: JournalEntry, normalizedQuery: string): boolean {
  if (!normalizedQuery) return true;
  return [
    entry.title,
    entry.text,
    entry.status,
    statusLabel(entry.status),
    entry.category,
    CATEGORY_LABELS[entry.category],
    entry.origin.place,
    entry.origin.actor,
    entry.origin.citation,
    ...(entry.actor_aliases ?? []),
  ].filter(Boolean).join(" ").toLocaleLowerCase().includes(normalizedQuery);
}
