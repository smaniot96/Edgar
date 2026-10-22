import { expect, it } from "vitest";
import { currentThread, indexJournal } from "./model";
import type { JournalEntry, JournalRead } from "./types";

function entry(id: string, category: JournalEntry["category"], turn: number): JournalEntry {
  return { id, category, title: id, text: null, status: "discovered", quantity: null, related_entry_id: null, history: [],
    origin: { turn_number: turn, history_event_id: turn, place: null, actor: null, citation: null } };
}

it("respects the server's absence of a lead and falls back only for older responses", () => {
  const clue = entry("clue", "clue", 3);
  const data: JournalRead = { session_id: 1, tracking: "available", current_place: null, active_goal: null, entries: [clue] };
  expect(currentThread(data)?.id).toBe("clue");
  expect(currentThread({ ...data, current_lead: null })).toBeNull();
});

it("orders the log by earned status updates while preserving original provenance", () => {
  const person = entry("Mara", "person", 1);
  person.status = "released";
  person.history = [{ status: "released", turn_number: 9, history_event_id: 9 }];
  const index = indexJournal({ session_id: 1, tracking: "available", current_place: null, active_goal: null, entries: [entry("clue", "clue", 8), person] });
  expect(index.chronological[0].id).toBe("Mara");
  expect(index.chronological[0].origin.turn_number).toBe(1);
});
