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
