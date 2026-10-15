// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import WorldTab from "./WorldTab";

const harness = vi.hoisted(() => ({ isDev: false }));
vi.mock("../../dev/ModeContext", () => ({ useMode: () => ({ isDev: harness.isDev }) }));
vi.mock("../../api/client", () => ({
  ApiError: class ApiError extends Error {
    status: number;
    constructor(status: number, message: string) { super(message); this.status = status; }
  },
  createWorldFlag: vi.fn(),
  deleteWorldFlag: vi.fn(),
  listWorldFlags: vi.fn(() => new Promise(() => {})),
  updateWorldFlag: vi.fn(),
}));
import { listWorldFlags, updateWorldFlag } from "../../api/client";

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

it("keeps a failed edit as a draft, supports cancel, and shows safe outcomes", async () => {
  harness.isDev = true;
  vi.mocked(listWorldFlags).mockResolvedValue([
    { id: 1, campaign_id: 7, key: "gate", value: "closed" },
  ]);
  vi.mocked(updateWorldFlag).mockRejectedValue(new Error("PRIVATE SERVER EXCEPTION"));
  await act(async () => {
    root.render(<MemoryRouter><WorldTab campaignId={7} /></MemoryRouter>);
  });
  await act(async () => { await Promise.resolve(); });

  const input = container.querySelector<HTMLInputElement>("#flag-1");
  expect(input).not.toBeNull();
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
  await act(() => {
    setter?.call(input, "open");
    input?.dispatchEvent(new Event("input", { bubbles: true }));
  });
  expect(input?.value).toBe("open");

  await act(async () => {
    Array.from(container.querySelectorAll("button")).find((button) => button.textContent === "Save")?.click();
    await Promise.resolve();
    await Promise.resolve();
  });
  expect(container.textContent).toContain("The flag change could not be saved. Try again.");
  expect(container.textContent).not.toContain("PRIVATE SERVER EXCEPTION");
  expect(input?.value).toBe("open");

  await act(() => {
    Array.from(container.querySelectorAll("button")).find((button) => button.textContent === "Cancel")?.click();
  });
  expect(input?.value).toBe("closed");
  expect(container.textContent).not.toContain("Cancel");
});
