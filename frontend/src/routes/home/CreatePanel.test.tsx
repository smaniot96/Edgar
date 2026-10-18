// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import type { CharacterListItem } from "../../api/types";
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

const freeHero: CharacterListItem = {
  id: 1, name: "Mara Venn", character_class: "Ranger", level: 2, hp_max: 18,
  base_stats: {}, base_inventory: {}, created_at: "2026-10-01", current_assignment: null,
};
const busyHero: CharacterListItem = {
  ...freeHero, id: 2, name: "Gianni Ferro",
  current_assignment: { campaign_id: 9, campaign_title: "Old Road" },
};

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.clearAllMocks();
  api.listCharacters.mockResolvedValue([freeHero, busyHero]);
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

it("offers heroes already adventuring elsewhere as unavailable in the preparation picker", async () => {
  await act(() => root.render(<CreatePanel onCreated={vi.fn()} />));
  await settle();
  const options = [...container.querySelectorAll<HTMLOptionElement>("select option")];
  const free = options.find((option) => option.textContent?.includes("Mara Venn"))!;
  const busy = options.find((option) => option.textContent?.includes("Gianni Ferro"))!;
  expect(free.disabled).toBe(false);
  expect(busy.disabled).toBe(true);
  expect(busy.textContent).toContain("adventuring in Old Road");
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
