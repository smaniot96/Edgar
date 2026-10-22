// @vitest-environment jsdom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { expect, it, vi } from "vitest";
import { JournalPanel } from "./JournalPanel";
import type { JournalRead } from "./types";

it("does not pull a reader back to the selected note on unrelated rerenders", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => { callback(0); return 1; });
  vi.stubGlobal("cancelAnimationFrame", vi.fn());
  const scroll = vi.fn();
  const previous = HTMLElement.prototype.scrollIntoView;
  HTMLElement.prototype.scrollIntoView = scroll;
  const container = document.createElement("div"); document.body.append(container);
  const root = createRoot(container);
  const data: JournalRead = { session_id: 1, tracking: "available", current_place: null, active_goal: null, entries: [{
    id: "earned-note", category: "clue", title: "Note", text: "Exact quotation", status: "discovered", quantity: null,
    origin: { turn_number: 1, history_event_id: 1, place: null, actor: null, citation: null }, history: [], related_entry_id: null,
  }] };
  const render = () => root.render(<JournalPanel data={structuredClone(data)} loading={false} error={null} stale={false} onRefresh={() => {}}
    selectedEntryId="earned-note" onSelectedEntryChange={() => {}} view="clues" onViewChange={() => {}} query="" onQueryChange={() => {}} />);
  try {
    await act(render); expect(scroll).toHaveBeenCalledOnce();
    await act(render); expect(scroll).toHaveBeenCalledOnce();
  } finally {
    await act(() => root.unmount()); container.remove(); HTMLElement.prototype.scrollIntoView = previous; vi.unstubAllGlobals();
  }
});

it("loads a large safe journal incrementally and reveals a selected later entry", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => { callback(0); return 1; });
  vi.stubGlobal("cancelAnimationFrame", vi.fn());
  const container = document.createElement("div"); document.body.append(container);
  const root = createRoot(container);
  const data: JournalRead = {
    session_id: 8,
    tracking: "available",
    current_place: null,
    active_goal: null,
    entries: Array.from({ length: 60 }, (_, index) => ({
      id: `note-${index}`,
      category: "clue" as const,
      title: `Clue ${index}`,
      text: index === 0 ? "A long earned quotation stays readable when opened directly." : `Earned clue ${index}`,
      status: "discovered" as const,
      quantity: null,
      origin: { turn_number: index + 1, history_event_id: index + 1, place: null, actor: null, citation: null },
      history: [],
      related_entry_id: null,
    })),
  };
  const render = (selectedEntryId: string | null = null) => root.render(
    <JournalPanel data={data} loading={false} error={null} stale={false} onRefresh={() => {}}
      selectedEntryId={selectedEntryId} onSelectedEntryChange={() => {}} view="clues" onViewChange={() => {}}
      query="" onQueryChange={() => {}} />,
  );
  try {
    await act(() => render());
    expect(container.querySelectorAll("article")).toHaveLength(24);
    expect(container.textContent).not.toContain("60 entries");
    const more = () => Array.from(container.querySelectorAll("button")).find((button) => button.textContent === "Show 24 more");
    await act(() => more()?.click());
    expect(container.querySelectorAll("article")).toHaveLength(48);
    await act(() => render("note-0"));
    expect(container.querySelectorAll("article")).toHaveLength(60);
    expect(container.querySelector("#journal-entry-note-0")?.getAttribute("aria-current")).toBe("true");
    expect(container.textContent).toContain("A long earned quotation stays readable when opened directly.");
  } finally {
    await act(() => root.unmount()); container.remove(); vi.unstubAllGlobals();
  }
});

it("shows ownership and can follow consequence links across active category filters", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  const container = document.createElement("div"); document.body.append(container);
  const root = createRoot(container);
  const onSelectedEntryChange = vi.fn();
  const onCategoryChange = vi.fn();
  const onQueryChange = vi.fn();
  const data: JournalRead = {
    session_id: 9,
    tracking: "available",
    current_place: null,
    active_goal: null,
    entries: [
      {
        id: "change-id", category: "consequence", title: "The ward changes", text: "The ward dims.",
        status: "changed", quantity: null, origin: { turn_number: 3, history_event_id: 3, place: null, actor: null, citation: null },
        history: [], related_entry_id: null, related_entry_ids: ["clue-id", "place-id"],
      },
      {
        id: "clue-id", category: "clue", title: "Lens note", text: "The lens tracks reflections.",
        status: "discovered", quantity: null, ownership: "carried",
        origin: { turn_number: 1, history_event_id: 1, place: null, actor: null, citation: null },
        history: [], related_entry_id: null,
      },
      {
        id: "place-id", category: "place", title: "Star pool", text: "A quiet pool.",
        status: "known", quantity: null,
        origin: { turn_number: 2, history_event_id: 2, place: null, actor: null, citation: null },
        history: [], related_entry_id: null,
      },
      {
        id: "clock-id", category: "clock", title: "Rising water", text: "The water is rising.",
        status: "advanced", quantity: null, clock_maximum: 6,
        origin: { turn_number: 1, history_event_id: 1, place: null, actor: null, citation: null },
        history: [], related_entry_id: null,
      },
    ],
  };
  const render = (category: "all" | "consequence", query = "ward") => root.render(
    <JournalPanel data={data} loading={false} error={null} stale={false} onRefresh={() => {}}
      selectedEntryId={null} onSelectedEntryChange={onSelectedEntryChange} view={category === "consequence" ? "log" : "overview"}
      onViewChange={onCategoryChange} query={query} onQueryChange={onQueryChange} />,
  );
  try {
    await act(() => render("consequence"));
    expect(container.textContent).toContain("Lens note");
    expect(container.textContent).toContain("Star pool");
    await act(() => Array.from(container.querySelectorAll("button")).find((button) => button.getAttribute("aria-label") === "Open clue: Lens note")?.click());
    expect(onCategoryChange).toHaveBeenCalledWith("clues");
    expect(onQueryChange).toHaveBeenCalledWith("");
    expect(onSelectedEntryChange).toHaveBeenCalledWith("clue-id");

    await act(() => root.render(<JournalPanel data={data} loading={false} error={null} stale={false} onRefresh={() => {}} selectedEntryId={null} onSelectedEntryChange={onSelectedEntryChange} view="clues" onViewChange={onCategoryChange} query="" onQueryChange={onQueryChange} />));
    expect(container.textContent).toContain("You are carrying it.");
    await act(() => root.render(<JournalPanel data={data} loading={false} error={null} stale={false} onRefresh={() => {}} selectedEntryId={null} onSelectedEntryChange={onSelectedEntryChange} view="places" onViewChange={onCategoryChange} query="" onQueryChange={onQueryChange} />));
    expect(container.textContent).toContain("heard of, not visited");
    await act(() => root.render(<JournalPanel data={data} loading={false} error={null} stale={false} onRefresh={() => {}} selectedEntryId={null} onSelectedEntryChange={onSelectedEntryChange} view="goals" onViewChange={onCategoryChange} query="" onQueryChange={onQueryChange} />));
    expect(container.textContent).toContain("Known maximum: 6");
  } finally {
    await act(() => root.unmount()); container.remove(); vi.unstubAllGlobals();
  }
});

it("keeps keyboard focus on the first entry in a revealed batch", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  const container = document.createElement("div"); document.body.append(container);
  const root = createRoot(container);
  const data: JournalRead = {
    session_id: 4, tracking: "available", current_place: null, active_goal: null,
    entries: Array.from({ length: 30 }, (_, index) => ({
      id: `clue-${index}`, category: "clue", title: `Clue ${index}`, text: `Earned ${index}`,
      status: "discovered", quantity: null, related_entry_id: null, history: [],
      origin: { turn_number: 30 - index, history_event_id: index + 1, place: null, actor: null, citation: null },
    })),
  };
  try {
    await act(() => root.render(<JournalPanel data={data} loading={false} error={null} stale={false} onRefresh={() => {}} onOpenHistory={() => {}}
      selectedEntryId={null} onSelectedEntryChange={() => {}} view="clues" onViewChange={() => {}} query="" onQueryChange={() => {}} />));
    expect(container.querySelector('button[aria-label="Read the moment: Clue 0"]')).not.toBeNull();
    const more = Array.from(container.querySelectorAll("button")).find((item) => item.textContent === "Show 6 more")!;
    more.focus();
    await act(() => more.click());
    await act(() => new Promise<void>((resolve) => requestAnimationFrame(() => resolve())));
    expect(container.querySelectorAll("article")).toHaveLength(30);
    expect(document.activeElement?.id).toBe("journal-entry-clue-24");
  } finally {
    await act(() => root.unmount()); container.remove(); vi.unstubAllGlobals();
  }
});

it("reveals and focuses an earned answer nested inside a later clue", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  const container = document.createElement("div"); document.body.append(container);
  const root = createRoot(container);
  const clues = Array.from({ length: 30 }, (_, index) => ({
    id: `clue-${index}`, category: "clue" as const, title: `Clue ${index}`, text: `Source ${index}`,
    status: "discovered" as const, quantity: null, related_entry_id: null, history: [],
    origin: { turn_number: 30 - index, history_event_id: index + 1, place: null, actor: null, citation: null },
  }));
  const answer = { ...clues[29], id: "answer", category: "note" as const, title: "The keeper can help", text: "The keeper can help with the flood.", status: "learned" as const, related_entry_ids: ["clue-29"] };
  const data: JournalRead = { session_id: 1, tracking: "available", current_place: null, active_goal: null, current_lead: answer, entries: [...clues, answer] };
  try {
    await act(() => root.render(<JournalPanel data={data} loading={false} error={null} stale={false} onRefresh={() => {}}
      selectedEntryId="answer" onSelectedEntryChange={() => {}} view="clues" onViewChange={() => {}} query="" onQueryChange={() => {}} />));
    const target = container.querySelector<HTMLElement>("#journal-entry-answer");
    expect(target?.textContent).toContain("The keeper can help with the flood.");
    expect(target?.getAttribute("aria-current")).toBe("true");
    expect(target?.closest("article")?.id).toBe("journal-entry-clue-29");
    expect(container.querySelectorAll("#journal-entry-answer")).toHaveLength(1);
  } finally { await act(() => root.unmount()); container.remove(); vi.unstubAllGlobals(); }
});

it("searches nested earned answers without duplicating their DOM identities", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  const container = document.createElement("div"); document.body.append(container);
  const root = createRoot(container);
  const origin = { turn_number: 1, history_event_id: 1, place: null, actor: null, citation: null };
  const base = { quantity: null, related_entry_id: null, history: [], origin };
  const data: JournalRead = { session_id: 1, tracking: "available", current_place: null, active_goal: null, entries: [
    { ...base, id: "clue", category: "clue", title: "Flood marks", text: "Marks of a flood.", status: "discovered" },
    { ...base, id: "answer", category: "note", title: "Flood cause", text: "The flood started before dawn.", status: "learned", related_entry_ids: ["clue"] },
  ] };
  try {
    await act(() => root.render(<JournalPanel data={data} loading={false} error={null} stale={false} onRefresh={() => {}}
      selectedEntryId={null} onSelectedEntryChange={() => {}} view="people" onViewChange={() => {}} query="flood" onQueryChange={() => {}} />));
    expect(container.querySelectorAll("article")).toHaveLength(2);
    expect(container.querySelectorAll("#journal-entry-answer")).toHaveLength(1);
    expect(container.textContent?.match(/The flood started before dawn\./g)).toHaveLength(1);
  } finally { await act(() => root.unmount()); container.remove(); vi.unstubAllGlobals(); }
});
