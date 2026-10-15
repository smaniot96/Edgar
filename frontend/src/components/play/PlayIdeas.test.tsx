// @vitest-environment jsdom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { expect, it, vi } from "vitest";
import type { JournalEntry, JournalRead } from "../journal/types";
import { PlayIdeas } from "./PlayIdeas";

it("offers earned optional examples through explicit append controls without submitting", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  const onAppend = vi.fn(); const onSubmit = vi.fn();
  const place = { id: "known", category: "place", title: "Star pool" } as JournalEntry;
  const data: JournalRead = { session_id: 1, tracking: "available", entries: [place], current_place: place, active_goal: null };
  const container = document.createElement("div"); document.body.append(container); const root = createRoot(container);
  try {
    await act(() => root.render(<form onSubmit={onSubmit}><PlayIdeas data={data} hasDraft onAppend={onAppend} /></form>));
    expect(container.textContent).toContain("Optional examples");
    expect(container.textContent).toContain("Append to draft");
    await act(() => container.querySelector<HTMLButtonElement>("button")!.click());
    expect(onAppend).toHaveBeenCalledExactlyOnceWith("I inspect the surroundings at Star pool.");
    expect(onSubmit).not.toHaveBeenCalled();
    await act(() => root.render(<PlayIdeas data={null} hasDraft={false} onAppend={onAppend} />));
    expect(container.textContent).toContain("not enough recorded context");
    expect(container.querySelector("button")).toBeNull();
  } finally { await act(() => root.unmount()); container.remove(); vi.unstubAllGlobals(); }
});
