// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import type { CampaignRead, ReadinessRead, SessionRead } from "../api/types";
import CampaignView from "./Campaign";

const api = vi.hoisted(() => ({
  deleteCampaign: vi.fn(),
  endCampaign: vi.fn(),
  getAdventure: vi.fn(),
  getCampaign: vi.fn(),
  isNotFound: vi.fn(() => false),
  listSessions: vi.fn(),
  reopenCampaign: vi.fn(),
  setCampaignRulesEdition: vi.fn(),
}));
vi.mock("../api/client", async () => ({ ...(await vi.importActual<object>("../api/client")), ...api }));
vi.mock("../dev/ModeContext", () => ({ useMode: () => ({ isDev: false }) }));

const blocked: ReadinessRead = {
  play_allowed: false, history_allowed: true, state: "source_missing", primary_action: "recover_source",
  blockers: [{ code: "source_missing", message: "Restore the adventure source before continuing play.", action: "recover_source" }],
};
const campaign: CampaignRead = {
  id: 12, title: "The Old Road", system: "5e", created_by: 1, created_at: "2026-10-15",
  adventure_collections: [], source_mode: "homebrew", status: "active", ended_at: null,
  retrieval_degraded: false, play_allowed: false, readiness: blocked,
};
const savedStory: SessionRead = {
  id: 88, campaign_id: 12, started_at: "2026-10-14T10:00:00Z", ended_at: null,
  current_scene_id: null, active_character_id: 30, character_name: "Mara Venn",
  has_history: true, last_played_at: "2026-10-16T10:00:00Z", readiness: blocked,
};

let container: HTMLDivElement;
let root: Root;

async function renderCampaign(sessions: SessionRead[]) {
  api.listSessions.mockResolvedValue(sessions);
  await act(async () => {
    root.render(<MemoryRouter initialEntries={["/campaigns/12"]}><Routes><Route path="/campaigns/:id" element={<CampaignView />} /></Routes></MemoryRouter>);
    await Promise.resolve();
  });
  await act(async () => { await Promise.resolve(); await Promise.resolve(); });
}

beforeEach(() => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.clearAllMocks();
  api.getCampaign.mockResolvedValue(campaign);
  api.getAdventure.mockResolvedValue(null);
  api.listSessions.mockResolvedValue([savedStory]);
  api.endCampaign.mockResolvedValue(campaign);
  api.deleteCampaign.mockResolvedValue(undefined);
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

it("puts readable saved story and the real recovery blocker before collapsed administration", async () => {
  await renderCampaign([savedStory]);
  expect(container.textContent).toContain("Mara Venn");
  expect([...container.querySelectorAll("button")].some((button) => button.textContent?.trim() === "Read story")).toBe(true);
  expect(container.textContent).toContain("Restore the adventure source before continuing play.");
  const settings = container.querySelector("details")!;
  expect(settings.open).toBe(false);
  expect(settings.querySelector("button[aria-label='Delete campaign and all its saved playthroughs']")).not.toBeNull();
  expect([...container.querySelectorAll("button")].some((button) => button.textContent?.trim() === "End campaign" && !settings.contains(button))).toBe(false);
});

it("offers first-session setup before administration when the campaign has no saves", async () => {
  api.getCampaign.mockResolvedValue({ ...campaign, readiness: { ...blocked, play_allowed: true }, play_allowed: true });
  api.listSessions.mockResolvedValue([]);
  await renderCampaign([]);
  expect([...container.querySelectorAll("a")].some((link) => link.textContent?.includes("Set up first session"))).toBe(true);
  expect(container.textContent).toContain("No saved playthrough yet");
  expect(container.querySelector("details")?.open).toBe(false);
});

it("keeps end confirmation as a deliberate secondary action", async () => {
  const confirm = vi.fn(() => false);
  vi.stubGlobal("confirm", confirm);
  await renderCampaign([savedStory]);
  const settings = container.querySelector("details")!;
  await act(async () => {
    settings.open = true;
    settings.dispatchEvent(new Event("toggle"));
  });
  const end = [...container.querySelectorAll("button")].find((button) => button.textContent?.trim() === "End campaign")!;
  await act(async () => end.click());
  expect(confirm).toHaveBeenCalledOnce();
  expect(api.endCampaign).not.toHaveBeenCalled();
});
