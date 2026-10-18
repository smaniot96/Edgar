// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import type { AdventureRead, CampaignRead, CharacterListItem } from "../api/types";
import Characters from "./Characters";

const api = vi.hoisted(() => ({
  listAdventures: vi.fn(),
  listCampaigns: vi.fn(),
  listCharacterPresets: vi.fn(),
  listCharacters: vi.fn(),
}));

vi.mock("../api/client", async () => ({
  ...(await vi.importActual<object>("../api/client")),
  ...api,
}));
vi.mock("../dev/ModeContext", () => ({ useMode: () => ({ isDev: false }) }));

const gianni: CharacterListItem = {
  id: 4, name: "Gianni", character_class: "Rogue", level: 3, hp_max: 22,
  base_stats: {}, base_inventory: {}, created_at: "2026-10-02",
  current_assignment: { campaign_id: 7, campaign_title: "Solo Session" },
};
const campaign: CampaignRead = {
  id: 7, title: "Solo Session", system: "5e", created_by: 1, created_at: "2026-10-02",
  adventure_collections: ["lost_mine"], source_mode: "module", status: "active", ended_at: null,
  retrieval_degraded: false, play_allowed: true, sources: [],
};
const adventure: AdventureRead = {
  slug: "lost_mine", title: "Lost Mine of Phandelver", description: null, level_range: null, cover_image: null, status: "ready",
};

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.clearAllMocks();
  api.listCharacters.mockResolvedValue([gianni]);
  api.listCharacterPresets.mockResolvedValue([]);
  api.listCampaigns.mockResolvedValue([campaign]);
  api.listAdventures.mockResolvedValue([adventure]);
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(() => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
});

it("names the campaign a hero is in rather than the session title stored on the campaign", async () => {
  await act(() => root.render(<MemoryRouter><Characters /></MemoryRouter>));
  await act(async () => { await Promise.resolve(); await Promise.resolve(); });
  expect(container.textContent).toContain("In: Lost Mine of Phandelver");
  expect(container.textContent).not.toContain("In: Solo Session");
});
