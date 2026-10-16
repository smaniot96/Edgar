// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { Link, MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { ApiError } from "../api/client";
import type { CampaignRead, CharacterListItem, CharacterPreset, CharacterRead, ReadinessRead, SessionRead } from "../api/types";
import { clearCampaignSetupProgress, writeCampaignSetupProgress, newCampaignSetupProgress } from "../lib/campaignSetup";
import CampaignSetup from "./CampaignSetup";

const api = vi.hoisted(() => ({
  assignCharacterToCampaign: vi.fn(),
  createCharacter: vi.fn(),
  createSession: vi.fn(),
  getCampaign: vi.fn(),
  listCharacterPresets: vi.fn(),
  listCharacters: vi.fn(),
  listSessions: vi.fn(),
}));

vi.mock("../api/client", async () => ({
  ...(await vi.importActual<object>("../api/client")),
  ...api,
}));
vi.mock("../dev/ModeContext", () => ({ useMode: () => ({ isDev: false }) }));

const ready: ReadinessRead = { play_allowed: true, history_allowed: true, state: "ready", primary_action: "play", blockers: [] };
const campaign: CampaignRead = {
  id: 12, title: "The Old Road", system: "5e", created_by: 1, created_at: "2026-10-15",
  adventure_collections: ["old_road"], source_mode: "module", status: "active", ended_at: null,
  retrieval_degraded: false, play_allowed: true, readiness: ready, sources: [],
};
const preset: CharacterPreset = {
  key: "fighter", label: "Fighter", character_class: "Fighter", level: 1,
  hp_max: 12, hp_current: 12, stats: { str: 15 }, inventory: { items: [] },
};
const existingCharacter: CharacterListItem = {
  id: 30, name: "Mara Venn", character_class: "Ranger", level: 2, hp_max: 18,
  base_stats: {}, base_inventory: {}, created_at: "2026-10-01", current_assignment: null,
};
const createdCharacter: CharacterRead = {
  id: 31, owner_user_id: 1, name: "Brannoc", character_class: "Fighter", level: 1,
  hp_max: 12, base_stats: { str: 15 }, base_inventory: { items: [] }, created_at: "2026-10-16",
};
const createdSession: SessionRead = {
  id: 88, campaign_id: campaign.id, started_at: "2026-10-16T10:00:00Z", ended_at: null,
  current_scene_id: null, active_character_id: existingCharacter.id, character_name: existingCharacter.name,
  has_history: false, readiness: ready,
};

let container: HTMLDivElement;
let root: Root;

function renderSetup() {
  root.render(
    <MemoryRouter initialEntries={[`/campaigns/${campaign.id}/setup`]}>
      <Routes>
        <Route path="/campaigns/:id/setup" element={<CampaignSetup />} />
        <Route path="/play/:sessionId" element={<p>Play route</p>} />
      </Routes>
    </MemoryRouter>,
  );
}

async function settle() {
  await act(async () => { await Promise.resolve(); await Promise.resolve(); });
}

beforeEach(() => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.clearAllMocks();
  window.localStorage.clear();
  clearCampaignSetupProgress(campaign.id);
  vi.spyOn(crypto, "randomUUID").mockReturnValue("11111111-1111-4111-8111-111111111111");
  api.getCampaign.mockResolvedValue(campaign);
  api.listCharacters.mockResolvedValue([existingCharacter]);
  api.listCharacterPresets.mockResolvedValue([preset]);
  api.listSessions.mockResolvedValue([]);
  api.assignCharacterToCampaign.mockResolvedValue(undefined);
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(() => root.unmount());
  container.remove();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

it("retains the assignment and the exact save request when session creation loses its response", async () => {
  api.createSession.mockRejectedValueOnce(new ApiError(0, "offline"));
  api.createSession.mockResolvedValueOnce(createdSession);
  await act(async () => { renderSetup(); await Promise.resolve(); });
  await settle();

  await act(async () => {
    [...container.querySelectorAll("button")].find((button) => button.textContent?.includes("Mara Venn"))!.click();
  });
  await act(async () => {
    [...container.querySelectorAll("button")].find((button) => button.textContent?.includes("Assign hero and continue"))!.click();
  });
  await settle();
  expect(container.textContent).toContain("Create your saved playthrough");
  await act(async () => {
    [...container.querySelectorAll("button")].find((button) => button.textContent?.includes("Start saved playthrough"))!.click();
    await Promise.resolve();
  });
  await settle();

  const saved = JSON.parse(window.localStorage.getItem(`edgar:campaign-setup:v1:${campaign.id}`)!);
  const requestId = saved.pendingSession.requestId;
  expect(saved.assignedCharacterId).toBe(existingCharacter.id);
  expect(saved.pendingSession.characterId).toBe(existingCharacter.id);
  expect(api.assignCharacterToCampaign).toHaveBeenCalledTimes(1);
  expect(api.createSession).toHaveBeenNthCalledWith(1, campaign.id, existingCharacter.id, requestId);

  await act(async () => { await root.unmount(); });
  root = createRoot(container);
  await act(async () => { renderSetup(); await Promise.resolve(); });
  await settle();
  expect(container.textContent).toContain("A saved playthrough request is retained");
  await act(async () => {
    [...container.querySelectorAll("button")].find((button) => button.textContent?.includes("Start saved playthrough"))!.click();
    await Promise.resolve();
  });
  await settle();

  expect(api.assignCharacterToCampaign).toHaveBeenCalledTimes(1);
  expect(api.createSession).toHaveBeenCalledTimes(2);
  expect(api.createSession).toHaveBeenNthCalledWith(2, campaign.id, existingCharacter.id, requestId);
  expect(container.textContent).toContain("Play route");
});

it("reuses a playthrough request ID written by another tab after this tab loaded", async () => {
  await act(async () => { renderSetup(); await Promise.resolve(); });
  await settle();
  await act(async () => {
    [...container.querySelectorAll("button")].find((button) => button.textContent?.includes("Mara Venn"))!.click();
  });
  await act(async () => {
    [...container.querySelectorAll("button")].find((button) => button.textContent?.includes("Assign hero and continue"))!.click();
  });
  await settle();

  const storageKey = `edgar:campaign-setup:v1:${campaign.id}`;
  const progressFromOtherTab = JSON.parse(window.localStorage.getItem(storageKey)!);
  progressFromOtherTab.pendingSession = {
    requestId: "22222222-2222-4222-8222-222222222222",
    characterId: existingCharacter.id,
  };
  window.localStorage.setItem(storageKey, JSON.stringify(progressFromOtherTab));

  await act(async () => {
    [...container.querySelectorAll("button")].find((button) => button.textContent?.includes("Start saved playthrough"))!.click();
    await Promise.resolve();
  });
  await settle();

  expect(api.createSession).toHaveBeenCalledWith(campaign.id, existingCharacter.id, "22222222-2222-4222-8222-222222222222");
});

it("retains progress but blocks creation until browser storage can retain the retry key", async () => {
  // A valid older snapshot must not replace newer same-tab progress after a quota failure.
  writeCampaignSetupProgress(newCampaignSetupProgress(campaign.id));
  const storage = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("quota exceeded"); });
  await act(async () => { renderSetup(); await Promise.resolve(); });
  await settle();
  await act(async () => { [...container.querySelectorAll("button")].find((button) => button.textContent?.includes("Mara Venn"))!.click(); });
  await act(async () => { [...container.querySelectorAll("button")].find((button) => button.textContent?.includes("Assign hero and continue"))!.click(); });
  await settle();
  await act(async () => { [...container.querySelectorAll("button")].find((button) => button.textContent?.includes("Start saved playthrough"))!.click(); });
  await settle();
  expect(api.createSession).not.toHaveBeenCalled();
  expect(container.textContent).toContain("No saved playthrough was created");
  storage.mockRestore();
  api.createSession.mockResolvedValueOnce(createdSession);
  await act(async () => { [...container.querySelectorAll("button")].find((button) => button.textContent?.includes("Start saved playthrough"))!.click(); });
  await settle();
  expect(api.createSession).toHaveBeenCalledTimes(1);
  expect(container.textContent).toContain("Play route");
});

it("keeps the selected hero while its assignment-stage lookup is still loading", async () => {
  api.listCharacters.mockReturnValue(new Promise(() => undefined));
  writeCampaignSetupProgress({
    ...newCampaignSetupProgress(campaign.id),
    stage: "assignment",
    characterId: existingCharacter.id,
  });
  await act(async () => { renderSetup(); await Promise.resolve(); });
  await settle();

  expect(container.textContent).toContain("Loading the selected hero…");
  expect([...container.querySelectorAll("button")].some((button) => button.textContent?.trim() === "Continue" || button.textContent?.includes("Continue without a hero"))).toBe(false);
  expect(api.createSession).not.toHaveBeenCalled();
});

it("keeps the selected hero and offers a retry when its assignment-stage lookup fails", async () => {
  api.listCharacters.mockRejectedValue(new ApiError(0, "offline"));
  writeCampaignSetupProgress({
    ...newCampaignSetupProgress(campaign.id),
    stage: "assignment",
    characterId: existingCharacter.id,
  });
  await act(async () => { renderSetup(); await Promise.resolve(); });
  await settle();

  expect(container.textContent).toContain("Check your heroes before continuing.");
  expect([...container.querySelectorAll("button")].some((button) => button.textContent?.includes("Check heroes again"))).toBe(true);
  expect([...container.querySelectorAll("button")].some((button) => button.textContent?.includes("Continue without a hero"))).toBe(false);
  expect(JSON.parse(window.localStorage.getItem(`edgar:campaign-setup:v1:${campaign.id}`)!).characterId).toBe(existingCharacter.id);
});

it("requires an explicit choice before dropping a selected hero missing from the roster", async () => {
  api.listCharacters.mockResolvedValue([]);
  writeCampaignSetupProgress({
    ...newCampaignSetupProgress(campaign.id),
    stage: "assignment",
    characterId: existingCharacter.id,
  });
  await act(async () => { renderSetup(); await Promise.resolve(); });
  await settle();

  expect(container.textContent).toContain("The selected hero is no longer in your hero list.");
  const continueWithoutHero = [...container.querySelectorAll("button")].find((button) => button.textContent?.includes("Continue without a hero"));
  expect(continueWithoutHero).toBeTruthy();
  expect(JSON.parse(window.localStorage.getItem(`edgar:campaign-setup:v1:${campaign.id}`)!).characterId).toBe(existingCharacter.id);
  await act(async () => { continueWithoutHero!.click(); });
  expect(JSON.parse(window.localStorage.getItem(`edgar:campaign-setup:v1:${campaign.id}`)!).characterId).toBeNull();
});

it("retries an ambiguous hero creation with the saved request key and original body", async () => {
  api.listCharacters.mockResolvedValue([]);
  api.createCharacter.mockRejectedValueOnce(new ApiError(0, "offline"));
  api.createCharacter.mockResolvedValueOnce(createdCharacter);
  await act(async () => { renderSetup(); await Promise.resolve(); });
  await settle();
  await act(async () => {
    [...container.querySelectorAll("button")].find((button) => button.textContent?.includes("Create a hero"))!.click();
  });
  await settle();
  const name = container.querySelector<HTMLInputElement>('input[name="name"]')
    ?? [...container.querySelectorAll<HTMLInputElement>("input")].find((input) => input.placeholder === "e.g. Brannoc the Bold");
  expect(name).toBeTruthy();
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(name, "Brannoc");
    name!.dispatchEvent(new Event("input", { bubbles: true }));
    [...container.querySelectorAll("button")].find((button) => button.textContent?.includes("Save hero and continue"))!.click();
    await Promise.resolve();
  });
  await settle();

  // Leave and return after an ambiguous response; changed presets must not change the retry body.
  await act(async () => { await root.unmount(); });
  root = createRoot(container);
  api.listCharacterPresets.mockResolvedValue([{ ...preset, hp_max: 20, level: 2 }]);
  await act(async () => { renderSetup(); await Promise.resolve(); });
  await settle();
  const firstCall = api.createCharacter.mock.calls[0];
  expect(firstCall?.[0]).toMatchObject({ name: "Brannoc", character_class: "Fighter", hp_max: 12 });
  expect(JSON.parse(window.localStorage.getItem(`edgar:campaign-setup:v1:${campaign.id}`)!).pendingCharacter.requestId).toBe(firstCall?.[1]);
  await act(async () => {
    [...container.querySelectorAll("button")].find((button) => button.textContent?.includes("Retry saved hero creation"))!.click();
    await Promise.resolve();
  });
  await settle();

  expect(api.createCharacter).toHaveBeenCalledTimes(2);
  expect(api.createCharacter.mock.calls[1]).toEqual(firstCall);
  expect(container.textContent).toContain("Add this hero to the campaign");
  expect(JSON.parse(window.localStorage.getItem(`edgar:campaign-setup:v1:${campaign.id}`)!).characterId).toBe(createdCharacter.id);
});


it("explains an assignment conflict and leaves the hero in its current campaign", async () => {
  const elsewhere = {
    ...existingCharacter,
    current_assignment: { campaign_id: 44, campaign_title: "The Glass Keep" },
  };
  api.assignCharacterToCampaign.mockRejectedValueOnce(new ApiError(409, JSON.stringify({ code: "character_assignment_conflict" })));
  api.listCharacters
    .mockResolvedValueOnce([existingCharacter])
    .mockResolvedValueOnce([existingCharacter])
    .mockResolvedValueOnce([elsewhere]);
  await act(async () => { renderSetup(); await Promise.resolve(); });
  await settle();
  await act(async () => {
    [...container.querySelectorAll("button")].find((button) => button.textContent?.includes("Mara Venn"))!.click();
  });
  await act(async () => {
    [...container.querySelectorAll("button")].find((button) => button.textContent?.includes("Assign hero and continue"))!.click();
    await Promise.resolve();
  });
  await settle();

  expect(container.textContent).toContain("The Glass Keep");
  expect(container.textContent).toContain("It was not moved from that campaign.");
  expect(api.assignCharacterToCampaign).toHaveBeenCalledTimes(1);
  expect(api.createSession).not.toHaveBeenCalled();
  expect(JSON.parse(window.localStorage.getItem(`edgar:campaign-setup:v1:${campaign.id}`)!).characterId).toBe(existingCharacter.id);
});

it("does not create a hero when the exact request cannot be persisted", async () => {
  api.listCharacters.mockResolvedValue([]);
  await act(async () => { renderSetup(); await Promise.resolve(); });
  await settle();
  await act(async () => { [...container.querySelectorAll("button")].find((button) => button.textContent?.includes("Create a hero"))!.click(); });
  await settle();
  const name = [...container.querySelectorAll<HTMLInputElement>("input")].find((input) => input.placeholder === "e.g. Brannoc the Bold")!;
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(name, "Brannoc");
    name.dispatchEvent(new Event("input", { bubbles: true }));
  });
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("quota exceeded"); });
  await act(async () => { [...container.querySelectorAll("button")].find((button) => button.textContent?.includes("Save hero and continue"))!.click(); });
  await settle();
  expect(api.createCharacter).not.toHaveBeenCalled();
  expect(container.textContent).toContain("No hero was created");
});

it("recovers a lost assignment response through the authoritative roster without assigning twice", async () => {
  const assigned = { ...existingCharacter, current_assignment: { campaign_id: campaign.id, campaign_title: campaign.title } };
  api.listCharacters.mockResolvedValueOnce([existingCharacter]).mockResolvedValueOnce([existingCharacter]).mockResolvedValue([assigned]);
  api.assignCharacterToCampaign.mockRejectedValueOnce(new ApiError(0, "offline"));
  await act(async () => { renderSetup(); await Promise.resolve(); });
  await settle();
  await act(async () => { [...container.querySelectorAll("button")].find((button) => button.textContent?.includes("Mara Venn"))!.click(); });
  await act(async () => { [...container.querySelectorAll("button")].find((button) => button.textContent?.includes("Assign hero and continue"))!.click(); });
  await settle();
  expect(api.assignCharacterToCampaign).toHaveBeenCalledTimes(1);
  expect(JSON.parse(window.localStorage.getItem(`edgar:campaign-setup:v1:${campaign.id}`)!).assignedCharacterId).toBe(existingCharacter.id);
  expect(container.textContent).toContain("Create your saved playthrough");
});

it("allows hero recovery after the server rejects an assignment before save creation", async () => {
  writeCampaignSetupProgress({ ...newCampaignSetupProgress(campaign.id), stage: "playthrough", characterId: 30, assignedCharacterId: 30 });
  api.createSession.mockRejectedValueOnce(new ApiError(409, JSON.stringify({ code: "character_not_assigned" })));
  await act(async () => { renderSetup(); await Promise.resolve(); });
  await settle();
  await act(async () => { [...container.querySelectorAll("button")].find((button) => button.textContent?.includes("Start saved playthrough"))!.click(); });
  await settle();
  expect(api.createSession).toHaveBeenCalledTimes(1);
  expect(container.textContent).toContain("No saved playthrough was created. Choose an available hero");
  expect(container.textContent).toContain("Choose who will explore");
});

it("loads the destination campaign progress when setup route IDs change in place", async () => {
  writeCampaignSetupProgress({ ...newCampaignSetupProgress(12), stage: "playthrough", characterId: 30, assignedCharacterId: 30,
    pendingSession: { requestId: "11111111-1111-4111-8111-111111111111", characterId: 30 } });
  api.getCampaign.mockImplementation(async (id: number) => ({ ...campaign, id }));
  await act(async () => { root.render(
    <MemoryRouter initialEntries={["/campaigns/12/setup"]}>
      <Link to="/campaigns/13/setup">Another campaign</Link>
      <Routes><Route path="/campaigns/:id/setup" element={<CampaignSetup />} /></Routes>
    </MemoryRouter>); });
  await settle();
  expect(container.textContent).toContain("A saved playthrough request is retained");
  await act(async () => { container.querySelector<HTMLAnchorElement>('a[href="/campaigns/13/setup"]')!.click(); });
  await settle();
  expect(container.textContent).toContain("Choose who will explore");
  expect(container.textContent).not.toContain("A saved playthrough request is retained");
  expect(api.createSession).not.toHaveBeenCalled();
});
