// @vitest-environment jsdom
// EDG-055 F15/F16: the campaign card deletes the campaign (then any source it alone used)
// through an in-app dialog; a card without a campaign deletes its adventure source.
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import type { AdventureRead, CampaignRead, SessionRead } from "../api/types";
import Home from "./Home";

const data = vi.hoisted(() => ({
  adventures: [] as unknown[],
  campaigns: [] as unknown[],
  sessions: [] as unknown[],
  refetch: null as unknown as () => void,
}));
const api = vi.hoisted(() => ({
  deleteAdventure: vi.fn(),
  deleteCampaignAndUnusedSources: vi.fn(),
}));
vi.mock("../api/client", async () => ({ ...(await vi.importActual<object>("../api/client")), ...api }));
vi.mock("../dev/ModeContext", () => ({ useMode: () => ({ isDev: false }) }));
vi.mock("../lib/useQuery", () => ({
  useQuery: (key: string) => ({
    data: key === "me" ? { id: 1, email: "player@example.test", display_name: "Player" }
      : key === "adventures" ? data.adventures
        : key === "campaigns" ? data.campaigns
          : key === "sessions" ? data.sessions : [],
    error: null, loading: false, refetch: data.refetch, setData: vi.fn(),
  }),
}));

const velmoor: AdventureRead = { slug: "velmoor", title: "Velmoor (PDF)", description: null, level_range: null, cover_image: null, status: "ready" };
const campaign: CampaignRead = {
  id: 7, title: "Velmoor (PDF)", system: "5e", created_by: 1, created_at: "2026-10-20",
  adventure_collections: ["velmoor"], source_mode: "module", status: "active", ended_at: null,
};
const saves = [1, 2].map((id) => ({ id, campaign_id: 7, started_at: "2026-10-20", has_history: false })) as SessionRead[];

let container: HTMLDivElement;
let root: Root;
const confirm = vi.fn(() => false);

beforeEach(() => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.stubGlobal("confirm", confirm);
  vi.clearAllMocks();
  data.refetch = vi.fn();
  data.adventures = [velmoor];
  data.campaigns = [campaign];
  data.sessions = saves;
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
});
afterEach(async () => {
  await act(() => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
});

async function renderHome() {
  await act(async () => root.render(<MemoryRouter><Home /></MemoryRouter>));
}

function button(scope: ParentNode, label: string) {
  return [...scope.querySelectorAll("button")].find((b) => b.textContent?.trim() === label)!;
}

function dialog() {
  return document.querySelector<HTMLElement>("[role='alertdialog']");
}

it("deletes the campaign and its unused source, then reports what happened", async () => {
  api.deleteCampaignAndUnusedSources.mockResolvedValue({ campaign_id: 7, sources: [{ slug: "velmoor", outcome: "removed" }] });
  await renderHome();

  await act(async () => button(container, "Delete campaign").click());

  expect(confirm).not.toHaveBeenCalled();
  expect(dialog()?.textContent).toContain("This permanently deletes the campaign “Velmoor (PDF)” and its 2 saved sessions.");
  expect(dialog()?.textContent).toContain("The adventure source “Velmoor (PDF)” is deleted too");
  await act(async () => button(dialog()!, "Delete campaign").click());

  expect(api.deleteCampaignAndUnusedSources).toHaveBeenCalledWith(7);
  expect(api.deleteAdventure).not.toHaveBeenCalled();
  expect(dialog()).toBeNull();
  expect(container.querySelector("[role='status']")?.textContent).toContain(
    "Deleted “Velmoor (PDF)” and its saved sessions. The adventure source “Velmoor (PDF)” was deleted too.",
  );
  expect(data.refetch).toHaveBeenCalled();
});

it("says why a source was kept", async () => {
  api.deleteCampaignAndUnusedSources.mockResolvedValue({ campaign_id: 7, sources: [{ slug: "velmoor", outcome: "retained" }] });
  await renderHome();
  await act(async () => button(container, "Delete campaign").click());
  await act(async () => button(dialog()!, "Delete campaign").click());
  expect(container.querySelector("[role='status']")?.textContent).toContain(
    "was kept because this prepared version is kept for saved games.",
  );
});

it("deletes every campaign the card stands for, so its last delete can release the source", async () => {
  const ended = { ...campaign, id: 3, status: "ended" as const, created_at: "2026-10-01" };
  data.campaigns = [campaign, ended];
  api.deleteCampaignAndUnusedSources
    .mockResolvedValueOnce({ campaign_id: 7, sources: [{ slug: "velmoor", outcome: "in_use" }] })
    .mockResolvedValueOnce({ campaign_id: 3, sources: [{ slug: "velmoor", outcome: "removed" }] });
  await renderHome();
  await act(async () => button(container, "Delete campaign").click());
  expect(dialog()?.textContent).toContain("2 campaigns of “Velmoor (PDF)” (1 ended)");
  await act(async () => button(dialog()!, "Delete campaign").click());
  expect(api.deleteCampaignAndUnusedSources.mock.calls).toEqual([[7], [3]]);
  expect(container.querySelector("[role='status']")?.textContent).toContain("was deleted too.");
});

it("deletes a campaign whose source is already gone without touching adventures", async () => {
  data.adventures = [];
  data.campaigns = [{ ...campaign, sources: [{ slug: "velmoor", title: "Velmoor (PDF)", status: "missing", usable_chunks: 0, expected_chunks: null, collection_exists: false, source_pdf_available: false }] }];
  api.deleteCampaignAndUnusedSources.mockResolvedValue({ campaign_id: 7, sources: [{ slug: "velmoor", outcome: "missing" }] });
  await renderHome();
  await act(async () => button(container, "Delete campaign").click());
  expect(dialog()?.textContent).not.toContain("adventure source");
  await act(async () => button(dialog()!, "Delete campaign").click());
  expect(api.deleteCampaignAndUnusedSources).toHaveBeenCalledWith(7);
  expect(api.deleteAdventure).not.toHaveBeenCalled();
  const status = container.querySelector("[role='status']")?.textContent;
  expect(status).toContain("Deleted “Velmoor (PDF)” and its saved sessions.");
  expect(status).not.toContain("adventure source");
});

it("deletes the adventure source for a card that has no campaign yet", async () => {
  data.campaigns = [];
  data.sessions = [];
  data.adventures = [{ ...velmoor, slug: "failed_upload", title: "Failed upload", status: "failed" }];
  api.deleteAdventure.mockResolvedValue(undefined);
  await renderHome();
  await act(async () => button(container, "Delete adventure").click());
  expect(dialog()?.textContent).toContain("Delete the adventure “Failed upload”?");
  await act(async () => button(dialog()!, "Delete adventure").click());
  expect(api.deleteAdventure).toHaveBeenCalledWith("failed_upload");
  expect(api.deleteCampaignAndUnusedSources).not.toHaveBeenCalled();
  expect(container.querySelector("[role='status']")?.textContent).toContain("Deleted the adventure “Failed upload”.");
});

it("keeps the dialog open with the failure so the player can retry or cancel", async () => {
  const { ApiError } = await vi.importActual<typeof import("../api/client")>("../api/client");
  api.deleteCampaignAndUnusedSources.mockRejectedValue(new ApiError(503, ""));
  await renderHome();
  await act(async () => button(container, "Delete campaign").click());
  await act(async () => button(dialog()!, "Delete campaign").click());
  expect(dialog()?.querySelector("[role='alert']")).not.toBeNull();
  expect(container.querySelector("[role='status']")?.textContent).toBe("");
  await act(async () => button(dialog()!, "Cancel").click());
  expect(dialog()).toBeNull();
});
