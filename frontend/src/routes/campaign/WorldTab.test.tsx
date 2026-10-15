// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import WorldTab from "./WorldTab";

const harness = vi.hoisted(() => ({ isDev: false }));
vi.mock("../../dev/ModeContext", () => ({ useMode: () => ({ isDev: harness.isDev }) }));
vi.mock("../../api/client", () => ({
  createWorldFlag: vi.fn(),
  deleteWorldFlag: vi.fn(),
  listWorldFlags: vi.fn(() => new Promise(() => {})),
  updateWorldFlag: vi.fn(),
}));
import { listWorldFlags } from "../../api/client";

let container: HTMLDivElement;
let root: Root;
beforeEach(() => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.clearAllMocks();
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
});
afterEach(async () => {
  await act(() => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
});

it("keeps raw campaign overrides out of ordinary Player mode", async () => {
  harness.isDev = false;
  await act(() => root.render(<MemoryRouter><WorldTab campaignId={7} /></MemoryRouter>));
  expect(container.textContent).not.toContain("Set a flag");
  expect(container.querySelector('input[placeholder="bridge_destroyed"]')).toBeNull();
  expect(container.textContent).toContain("Journal");
  expect(listWorldFlags).not.toHaveBeenCalled();
});

it("keeps legacy overrides in a deliberate advanced tool", async () => {
  harness.isDev = true;
  await act(() => root.render(<MemoryRouter><WorldTab campaignId={7} /></MemoryRouter>));
  expect(container.textContent).toContain("Advanced world overrides");
  expect(container.textContent).toContain("Campaign 7");
  expect(container.textContent).toContain("do not rewrite committed journal facts");
  expect(container.querySelector('input[placeholder="bridge_destroyed"]')).not.toBeNull();
  expect(listWorldFlags).toHaveBeenCalledOnce();
});
