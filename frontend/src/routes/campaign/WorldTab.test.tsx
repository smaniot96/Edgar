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
import { createWorldFlag, deleteWorldFlag, listWorldFlags, updateWorldFlag } from "../../api/client";

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
  vi.restoreAllMocks();
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

function changeInput(input: HTMLInputElement, value: string) {
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
  setter?.call(input, value);
  input.dispatchEvent(new Event("input", { bubbles: true }));
}

async function settle() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

it("keeps the row through a failed update, reports success on retry, and warns on stale refresh", async () => {
  harness.isDev = true;
  vi.mocked(listWorldFlags)
    .mockResolvedValueOnce([{ id: 1, campaign_id: 7, key: "gate", value: "closed" }])
    .mockRejectedValueOnce(new Error("PRIVATE REFRESH FAILURE"));
  vi.mocked(updateWorldFlag)
    .mockRejectedValueOnce(new Error("PRIVATE UPDATE FAILURE"))
    .mockResolvedValueOnce({ id: 1, campaign_id: 7, key: "gate", value: "open" });
  await act(async () => {
    root.render(<MemoryRouter><WorldTab campaignId={7} /></MemoryRouter>);
  });
  await settle();

  const input = container.querySelector<HTMLInputElement>("#flag-1")!;
  await act(() => changeInput(input, "open"));
  const save = () => Array.from(container.querySelectorAll("button"))
    .find((button) => button.textContent === "Save")?.click();
  await act(async () => { save(); await Promise.resolve(); await Promise.resolve(); });
  expect(container.textContent).toContain("The flag change could not be saved. Try again.");
  expect(container.querySelector("tbody")?.textContent).toContain("gate");
  expect(input.value).toBe("open");

  await act(async () => { save(); await Promise.resolve(); await Promise.resolve(); });
  await settle();
  expect(container.textContent).toContain("World flag updated.");
  expect(container.textContent).toContain("The list could not be refreshed. Showing the last loaded world overrides.");
  expect(container.textContent).not.toContain("PRIVATE UPDATE FAILURE");
  expect(container.textContent).not.toContain("PRIVATE REFRESH FAILURE");
  expect(container.querySelector("tbody")?.textContent).toContain("gate");
  expect(updateWorldFlag).toHaveBeenCalledTimes(2);
});

it("creates a flag, reports success, and clears the key/value draft", async () => {
  harness.isDev = true;
  vi.mocked(listWorldFlags)
    .mockResolvedValueOnce([])
    .mockResolvedValueOnce([{ id: 2, campaign_id: 7, key: "bridge_open", value: "true" }]);
  vi.mocked(createWorldFlag).mockResolvedValue({ id: 2, campaign_id: 7, key: "bridge_open", value: "true" });
  await act(async () => {
    root.render(<MemoryRouter><WorldTab campaignId={7} /></MemoryRouter>);
  });
  await settle();

  const key = container.querySelector<HTMLInputElement>('input[placeholder="bridge_destroyed"]')!;
  const value = container.querySelector<HTMLInputElement>('input[placeholder="true"]')!;
  await act(() => {
    changeInput(key, "bridge_open");
    changeInput(value, "true");
  });
  await act(async () => {
    container.querySelector("form")?.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    await Promise.resolve();
    await Promise.resolve();
  });
  await settle();
  expect(createWorldFlag).toHaveBeenCalledWith(7, { key: "bridge_open", value: "true" });
  expect(container.textContent).toContain("World flag created.");
  expect(container.querySelector("tbody")?.textContent).toContain("bridge_open");
  expect(key.value).toBe("");
  expect(value.value).toBe("");
});

it("deletes a flag and reports success while refreshing the list", async () => {
  harness.isDev = true;
  vi.spyOn(window, "confirm").mockReturnValue(true);
  vi.mocked(listWorldFlags)
    .mockResolvedValueOnce([{ id: 3, campaign_id: 7, key: "old_bridge", value: "broken" }])
    .mockResolvedValueOnce([]);
  vi.mocked(deleteWorldFlag).mockResolvedValue(undefined);
  await act(async () => {
    root.render(<MemoryRouter><WorldTab campaignId={7} /></MemoryRouter>);
  });
  await settle();
  expect(container.querySelector("tbody")?.textContent).toContain("old_bridge");

  await act(async () => {
    container.querySelector<HTMLButtonElement>('button[aria-label="Delete flag old_bridge"]')?.click();
    await Promise.resolve();
    await Promise.resolve();
  });
  await settle();
  expect(deleteWorldFlag).toHaveBeenCalledWith(7, "old_bridge");
  expect(container.textContent).toContain("World flag deleted.");
  expect(container.querySelector("tbody")).toBeNull();
});

it("allows the new key/value draft to be canceled", async () => {
  harness.isDev = true;
  await act(async () => {
    root.render(<MemoryRouter><WorldTab campaignId={7} /></MemoryRouter>);
  });
  const key = container.querySelector<HTMLInputElement>('input[placeholder="bridge_destroyed"]')!;
  const value = container.querySelector<HTMLInputElement>('input[placeholder="true"]')!;
  await act(() => {
    changeInput(key, "draft_key");
    changeInput(value, "draft_value");
  });
  await act(() => {
    Array.from(container.querySelectorAll("button")).find((button) => button.textContent === "Cancel")?.click();
  });
  expect(key.value).toBe("");
  expect(value.value).toBe("");
  expect(container.textContent).not.toContain("Cancel");
  expect(createWorldFlag).not.toHaveBeenCalled();
});
