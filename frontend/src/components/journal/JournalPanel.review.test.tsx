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
  const render = () => root.render(<JournalPanel data={data} loading={false} error={null} stale={false} onRefresh={() => {}}
    selectedEntryId="earned-note" onSelectedEntryChange={() => {}} category="all" onCategoryChange={() => {}} query="" onQueryChange={() => {}} />);
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
      text: index === 59 ? "A long earned quotation stays readable when opened directly." : `Earned clue ${index}`,
      status: "discovered" as const,
      quantity: null,
      origin: { turn_number: index + 1, history_event_id: index + 1, place: null, actor: null, citation: null },
      history: [],
      related_entry_id: null,
    })),
  };
  const render = (selectedEntryId: string | null = null) => root.render(
    <JournalPanel data={data} loading={false} error={null} stale={false} onRefresh={() => {}}
      selectedEntryId={selectedEntryId} onSelectedEntryChange={() => {}} category="all" onCategoryChange={() => {}}
      query="" onQueryChange={() => {}} />,
  );
  try {
    await act(() => render());
    expect(container.querySelectorAll("article")).toHaveLength(24);
    expect(container.textContent).not.toContain("60 entries");
    const more = () => Array.from(container.querySelectorAll("button")).find((button) => button.textContent === "Show more entries");
    await act(() => more()?.click());
    expect(container.querySelectorAll("article")).toHaveLength(48);
    await act(() => render("note-59"));
    expect(container.querySelectorAll("article")).toHaveLength(60);
    expect(container.querySelector("#journal-entry-note-59")?.getAttribute("aria-current")).toBe("true");
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
      selectedEntryId={null} onSelectedEntryChange={onSelectedEntryChange} category={category}
      onCategoryChange={onCategoryChange} query={query} onQueryChange={onQueryChange} />,
  );
  try {
    await act(() => render("consequence"));
    expect(container.textContent).toContain("Clue: Lens note");
    expect(container.textContent).toContain("Place: Star pool");
    await act(() => Array.from(container.querySelectorAll("button")).find((button) => button.textContent === "Clue: Lens note")?.click());
    expect(onCategoryChange).toHaveBeenCalledWith("all");
    expect(onQueryChange).toHaveBeenCalledWith("");
    expect(onSelectedEntryChange).toHaveBeenCalledWith("clue-id");

    await act(() => render("all", ""));
    expect(container.textContent).toContain("In this character’s inventory");
    expect(container.textContent).toContain("known, not visited");
    expect(container.textContent).toContain("Known maximum: 6");
  } finally {
    await act(() => root.unmount()); container.remove(); vi.unstubAllGlobals();
  }
});
