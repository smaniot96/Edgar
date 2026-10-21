// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import type { CampaignRead, ReadinessRead, SessionRead } from "../api/types";
import CampaignView from "./Campaign";

const api = vi.hoisted(() => ({
  deleteCampaignAndUnusedSources: vi.fn(),
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
  api.deleteCampaignAndUnusedSources.mockResolvedValue({ campaign_id: 12, sources: [] });
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

async function openSettings() {
  const settings = container.querySelector("details")!;
  await act(async () => {
    settings.open = true;
    settings.dispatchEvent(new Event("toggle"));
  });
}

function button(scope: ParentNode, label: string) {
  return [...scope.querySelectorAll("button")].find((b) => b.textContent?.trim() === label)!;
}

function dialog() {
  return document.querySelector<HTMLElement>("[role='alertdialog']");
}

it("keeps end confirmation as a deliberate secondary action in an in-app dialog", async () => {
  // EDG-055 F15: embedded browsers suppress window.confirm, so it must never be used.
  const confirm = vi.fn(() => false);
  vi.stubGlobal("confirm", confirm);
  await renderCampaign([savedStory]);
  await openSettings();
  await act(async () => button(container, "End campaign").click());
  expect(confirm).not.toHaveBeenCalled();
  expect(dialog()?.textContent).toContain("Its characters are freed for other campaigns.");
  expect(api.endCampaign).not.toHaveBeenCalled();

  await act(async () => button(dialog()!, "Cancel").click());
  expect(dialog()).toBeNull();
  expect(api.endCampaign).not.toHaveBeenCalled();

  await act(async () => button(container, "End campaign").click());
  await act(async () => button(dialog()!, "End campaign").click());
  expect(api.endCampaign).toHaveBeenCalledWith(12);
  expect(dialog()).toBeNull();
});

it("deletes the campaign through the same dialog and hands the result to the campaign list", async () => {
  const confirm = vi.fn(() => false);
  vi.stubGlobal("confirm", confirm);
  let notice: unknown = null;
  function CampaignsPage() {
    notice = (useLocation().state as { notice?: string } | null)?.notice;
    return null;
  }
  api.listSessions.mockResolvedValue([savedStory]);
  await act(async () => {
    root.render(<MemoryRouter initialEntries={["/campaigns/12"]}><Routes>
      <Route path="/campaigns/:id" element={<CampaignView />} />
      <Route path="/campaigns" element={<CampaignsPage />} />
    </Routes></MemoryRouter>);
  });
  await act(async () => { await Promise.resolve(); await Promise.resolve(); });
  await openSettings();
  await act(async () => container.querySelector<HTMLButtonElement>("button[aria-label='Delete campaign and all its saved playthroughs']")!.click());

  expect(confirm).not.toHaveBeenCalled();
  expect(dialog()?.textContent).toContain("This permanently deletes the campaign “The Old Road” and its 1 saved session.");
  await act(async () => button(dialog()!, "Delete campaign").click());

  expect(api.deleteCampaignAndUnusedSources).toHaveBeenCalledWith(12);
  expect(notice).toBe("Deleted “The Old Road” and its saved sessions.");
});
