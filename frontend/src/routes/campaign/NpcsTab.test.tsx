// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter, Route, Routes, useSearchParams } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { listNpcs, listSessions } from "../../api/client";
import type { NPCListRead, SessionRead } from "../../api/types";
import NpcsTab from "./NpcsTab";

vi.mock("../../api/client", async (importOriginal) => ({
  ...await importOriginal<object>(),
  createNpc: vi.fn(),
  deleteNpc: vi.fn(),
  listNpcs: vi.fn(),
  listSessions: vi.fn(),
  updateNpc: vi.fn(),
}));
vi.mock("../../dev/ModeContext", () => ({ useMode: () => ({ isDev: false }) }));

const savedSessions: SessionRead[] = [
  { id: 1, campaign_id: 4, started_at: "2026-10-13T10:00:00Z", ended_at: null, current_scene_id: null, active_character_id: null },
  { id: 2, campaign_id: 4, started_at: "2026-10-14T10:00:00Z", ended_at: null, current_scene_id: null, active_character_id: null },
];

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

function UrlProbe() {
  const [params] = useSearchParams();
  return <output data-testid="query">{params.toString()}</output>;
}

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.clearAllMocks();
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  vi.mocked(listSessions).mockResolvedValue(savedSessions);
});

afterEach(async () => {
  await act(() => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
});

async function render(initialEntry = "/campaigns/4?tab=npcs") {
  await act(async () => {
    root.render(
      <MemoryRouter initialEntries={[initialEntry]}>
        <UrlProbe />
        <Routes>
          <Route path="/campaigns/:id" element={<NpcsTab campaignId={4} />} />
        </Routes>
      </MemoryRouter>,
    );
    await Promise.resolve();
    await Promise.resolve();
  });
}

function chooseSession(id: string) {
  const selector = container.querySelector<HTMLSelectElement>("#npc-session")!;
  selector.value = id;
  selector.dispatchEvent(new Event("change", { bubbles: true }));
}

it("keeps sessions separate when slower responses settle after a selection change", async () => {
  const first = deferred<NPCListRead>();
  const second = deferred<NPCListRead>();
  vi.mocked(listNpcs).mockImplementation((_campaignId, sessionId) => {
    if (sessionId === 1) return first.promise;
    if (sessionId === 2) return second.promise;
    return Promise.resolve({ npcs: [], tracking: "not_selected" });
  });

  await render();
  await act(async () => { chooseSession("1"); await Promise.resolve(); });
  await act(async () => { chooseSession("2"); await Promise.resolve(); });

  const current = {
    id: 22, campaign_id: 4, session_id: 2, name: "Odo", disposition: "friendly",
    stat_block: {}, journal_entry_id: "entry_odo",
  };
  await act(async () => {
    second.resolve({ npcs: [current], tracking: "available" });
    await second.promise;
  });
  expect(container.textContent).toContain("Odo");
  expect(container.textContent).not.toContain("Mara");
  expect(container.querySelector('a[aria-label="Open Odo in the story journal"]')?.getAttribute("href"))
    .toBe("/play/2?tool=journal&entry=entry_odo");
  expect(container.querySelector('[data-testid="query"]')?.textContent).toBe("tab=npcs&session_id=2");
  expect(listNpcs).toHaveBeenCalledWith(4, 2, expect.any(AbortSignal));

  await act(async () => {
    first.resolve({
      npcs: [{ ...current, id: 11, session_id: 1, name: "Mara", journal_entry_id: "entry_mara" }],
      tracking: "available",
    });
    await first.promise;
  });
  expect(container.textContent).toContain("Odo");
  expect(container.textContent).not.toContain("Mara");
});

it("labels the unselected campaign view as manual notes and does not promise automatic history", async () => {
  vi.mocked(listNpcs).mockResolvedValue({
    npcs: [{ id: 5, campaign_id: 4, session_id: null, name: "Prepared ally", disposition: "friendly", stat_block: {}, journal_entry_id: null }],
    tracking: "not_selected",
  });
  await render();
  expect(container.textContent).toContain("Campaign notes only");
  expect(container.textContent).toContain("Campaign notes · manual");
  expect(container.textContent).toContain("Prepared ally");
  expect(container.textContent).not.toContain("People Edgar learns through committed story updates will appear here");
  expect(listNpcs).toHaveBeenCalledWith(4, null, expect.any(AbortSignal));
});

it.each([
  ["legacy_untracked", "People were not recorded in this older save"],
  ["available", "No known people in this save"],
] as const)("distinguishes the %s empty state", async (tracking, expectedTitle) => {
  vi.mocked(listNpcs).mockImplementation((_campaignId, sessionId) =>
    Promise.resolve({ npcs: [], tracking: sessionId == null ? "not_selected" : tracking }),
  );
  await render();
  await act(async () => { chooseSession("1"); await Promise.resolve(); await Promise.resolve(); });
  expect(container.textContent).toContain(expectedTitle);
});

it("reports a failed session request without claiming the player knows nobody", async () => {
  vi.mocked(listNpcs).mockImplementation((_campaignId, sessionId) =>
    sessionId == null
      ? Promise.resolve({ npcs: [], tracking: "not_selected" })
      : Promise.reject(new Error("request failed")),
  );
  await render();
  await act(async () => { chooseSession("1"); await Promise.resolve(); await Promise.resolve(); });
  expect(container.textContent).toContain("Couldn't load this save's people list");
  expect(container.textContent).not.toContain("No known people in this save");
  expect(container.textContent).toContain("Retry");
});

it("shows edits for campaign notes and keeps learned rows system-managed", async () => {
  vi.mocked(listNpcs).mockImplementation((_campaignId, sessionId) =>
    Promise.resolve({
      npcs: sessionId == null
        ? [{ id: 5, campaign_id: 4, session_id: null, name: "Prepared ally", disposition: "friendly", stat_block: {}, journal_entry_id: null }]
        : [
            { id: 5, campaign_id: 4, session_id: null, name: "Prepared ally", disposition: "friendly", stat_block: {}, journal_entry_id: null },
            { id: 22, campaign_id: 4, session_id: 1, name: "Odo", disposition: "wary", stat_block: {}, journal_entry_id: "entry_odo" },
          ],
      tracking: sessionId == null ? "not_selected" : "available",
    }),
  );
  await render();
  await act(async () => { chooseSession("1"); await Promise.resolve(); await Promise.resolve(); });
  expect(container.querySelector('button[aria-label="Delete Prepared ally"]')).not.toBeNull();
  expect([...container.querySelectorAll("button")].some((button) => button.textContent === "Edit")).toBe(true);
  expect(container.querySelector('button[aria-label="Delete Odo"]')).toBeNull();
  expect(container.textContent).toContain("Tracked from this saved story");
});
