// @vitest-environment jsdom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { expect, it, vi } from "vitest";
import type { SessionRead } from "../../api/types";
import { SavedPlaythroughContext } from "./SavedPlaythroughContext";
const useJournal = vi.hoisted(() => vi.fn());
vi.mock("../journal/api", () => ({ useJournal }));
it("loads only the selected readable save and renders earned orientation without unrelated entries", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  const container = document.createElement("div"); document.body.append(container);
  const root = createRoot(container);
  const session = { id: 8, has_history: true, readiness: { history_allowed: true } } as SessionRead;
  useJournal.mockReturnValue({ data: { current_place: { title: "The known harbor" }, active_goal: { title: "Return the lantern" }, entries: [{ title: "An unrelated note" }] } });
  try {
    await act(() => root.render(<SavedPlaythroughContext session={session} />));
    expect(useJournal).toHaveBeenLastCalledWith(8);
    expect(container.textContent).toContain("Known place: The known harbor");
    expect(container.textContent).toContain("Goal: Return the lantern");
    expect(container.textContent).not.toContain("An unrelated note");
    useJournal.mockReturnValue({ data: null });
    await act(() => root.render(<SavedPlaythroughContext session={{ ...session, has_history: false }} />));
    expect(useJournal).toHaveBeenLastCalledWith(null);
    expect(container.textContent).toBe("");
    await act(() => root.render(<SavedPlaythroughContext session={{ ...session, readiness: { ...session.readiness!, history_allowed: false } }} />));
    expect(useJournal).toHaveBeenLastCalledWith(null);
  } finally { await act(() => root.unmount()); container.remove(); vi.unstubAllGlobals(); }
});
