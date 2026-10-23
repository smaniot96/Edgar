// @vitest-environment jsdom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { expect, it, vi } from "vitest";
import { TacticalGrid } from "./TacticalGrid";
import { previewCombatRoute } from "../../api/client";
import type { CombatRoutePreview, CombatState } from "../../api/types";

vi.mock("../../api/client", () => ({ previewCombatRoute: vi.fn() }));

const combat: CombatState = {
  round: 1, ended: false, outcome: null,
  battlefield: { width: 3, height: 1, square_ft: 5, theme: "open", terrain: [] },
  combatants: [{ id: "hero:1", name: "Mira", is_player: true, alive: true,
    hp_current: 20, hp_max: 20, position: { x: 0, y: 0, label: "A1" } }],
};
const route: CombatRoutePreview = {
  destination: "C1", budget_ft: 30, dash: false, reachable: true, reason: null,
  path: [{ x: 0, y: 0, label: "A1" }, { x: 1, y: 0, label: "B1" }, { x: 2, y: 0, label: "C1" }],
  cost_ft: 10, provokes: ["The guard"],
};

it("inspects creatures, previews empty squares, and only drafts on explicit request", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.mocked(previewCombatRoute).mockResolvedValue(route);
  const onPrepareMove = vi.fn(), onInspect = vi.fn();
  const host = document.createElement("div"); document.body.append(host); const root = createRoot(host);
  try {
    await act(() => root.render(<TacticalGrid combat={combat} sessionId={7} onPrepareMove={onPrepareMove} onInspect={onInspect} />));
    await act(() => host.querySelector<HTMLButtonElement>('[aria-label="A1, Mira"]')!.click());
    expect(onInspect).toHaveBeenCalledExactlyOnceWith("hero:1");
    expect(previewCombatRoute).not.toHaveBeenCalled();
    await act(() => host.querySelector<HTMLButtonElement>('[aria-label="C1"]')!.click());
    expect(previewCombatRoute).toHaveBeenCalledWith(7, "C1", false, expect.any(AbortSignal));
    expect(host.textContent).toContain("10 feet of 30 feet available");
    expect(host.textContent).toContain("Leaving reach can provoke: The guard");
    expect(onPrepareMove).not.toHaveBeenCalled();
    const draft = () => Array.from(host.querySelectorAll("button")).find((button) => button.textContent === "Add move to draft")!;
    await act(() => draft().click());
    expect(onPrepareMove).toHaveBeenCalledExactlyOnceWith("I move to C1.");
    await act(() => root.render(<TacticalGrid combat={{ ...combat, ended: true }} sessionId={7} onPrepareMove={onPrepareMove} onInspect={onInspect} />));
    expect(draft().disabled).toBe(true);
    expect(host.textContent).toContain("This encounter has ended");
  } finally { await act(() => root.unmount()); host.remove(); vi.unstubAllGlobals(); vi.clearAllMocks(); }
});

it("discards old previews on snapshot changes and ignores late responses", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  let resolveFirst!: (value: CombatRoutePreview) => void;
  let resolveNext!: (value: CombatRoutePreview) => void;
  vi.mocked(previewCombatRoute)
    .mockImplementationOnce(() => new Promise((resolve) => { resolveFirst = resolve; }))
    .mockImplementationOnce(() => new Promise((resolve) => { resolveNext = resolve; }));
  const host = document.createElement("div"); document.body.append(host); const root = createRoot(host);
  const props = { sessionId: 7, onPrepareMove: vi.fn(), onInspect: vi.fn() };
  const draft = () => Array.from(host.querySelectorAll("button")).find((button) => button.textContent === "Add move to draft")!;
  try {
    await act(() => root.render(<TacticalGrid combat={combat} {...props} />));
    await act(() => host.querySelector<HTMLButtonElement>('[aria-label="C1"]')!.click());
    await act(() => root.render(<TacticalGrid combat={{ ...combat, round: 2 }} {...props} />));
    await act(() => resolveFirst(route));
    expect(draft().disabled).toBe(true);
    expect(host.textContent).toContain("Checking the route");
    await act(() => resolveNext({ ...route, reachable: false, reason: "Blocked." }));
    expect(draft().disabled).toBe(true);
    expect(host.textContent).toContain("Blocked.");
    vi.mocked(previewCombatRoute).mockResolvedValueOnce(route);
    await act(() => host.querySelector<HTMLButtonElement>('[aria-label="C1"]')!.click());
    expect(draft().disabled).toBe(false);
    vi.mocked(previewCombatRoute).mockImplementationOnce(() => new Promise(() => {}));
    await act(() => root.render(<TacticalGrid combat={{ ...combat, round: 3 }} {...props} />));
    expect(draft().disabled).toBe(true); // A previously valid result is invalid immediately.
    expect(host.textContent).not.toContain("10 feet of 30 feet");
  } finally { await act(() => root.unmount()); host.remove(); vi.unstubAllGlobals(); vi.resetAllMocks(); }
});
