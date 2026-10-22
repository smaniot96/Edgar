// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { CreatePanel } from "./CreatePanel";

const api = vi.hoisted(() => ({
  generateAdventure: vi.fn(),
  listCharacters: vi.fn(),
  uploadAdventure: vi.fn(),
}));

vi.mock("../../api/client", async () => ({
  ...(await vi.importActual<object>("../../api/client")),
  ...api,
}));

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

async function settle() {
  await act(async () => { await Promise.resolve(); await Promise.resolve(); });
}

it("prepares a reusable adventure without requiring or loading a playthrough hero", async () => {
  const onCreated = vi.fn();
  api.generateAdventure.mockResolvedValue({ slug: "old_road", title: "Old Road" });
  await act(() => root.render(<CreatePanel onCreated={onCreated} />));
  await settle();
  expect(container.textContent).toContain("Choose or create your hero when you start a playthrough");
  const submit = [...container.querySelectorAll<HTMLButtonElement>("button")].find((button) => button.type === "submit")!;
  expect(submit.disabled).toBe(false);
  await act(async () => { container.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })); });
  expect(api.listCharacters).not.toHaveBeenCalled();
  expect(api.generateAdventure).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({
    size: "medium", preparation: { cost_target_microusd: 5_000_000 }, request_id: expect.any(String),
  }));
  expect(api.generateAdventure.mock.calls[0][0].preparation).not.toHaveProperty("character_id");
  expect(onCreated).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ slug: "old_road", status: "processing" }));
});

it("shows the chosen PDF name on the upload form", async () => {
  await act(() => root.render(<CreatePanel onCreated={vi.fn()} />));
  await settle();
  await act(async () => {
    [...container.querySelectorAll("button")].find((button) => button.textContent === "Upload a PDF")!.click();
  });
  expect(container.textContent).toContain("No file selected");
  const input = container.querySelector<HTMLInputElement>('input[type="file"]')!;
  const file = new File(["%PDF-1.4"], "old-road.pdf", { type: "application/pdf" });
  Object.defineProperty(input, "files", { value: [file], configurable: true });
  await act(async () => { input.dispatchEvent(new Event("change", { bubbles: true })); });
  expect(container.textContent).toContain("Selected: old-road.pdf");
});
