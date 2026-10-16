// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import type { ReadinessRead, SessionRead } from "../../api/types";
import SessionsTab from "./SessionsTab";

const api = vi.hoisted(() => ({
  listSessions: vi.fn(),
  listCharacters: vi.fn(),
  createSession: vi.fn(),
  assignCharacterToCampaign: vi.fn(),
  deleteSession: vi.fn(),
}));

vi.mock("../../api/client", () => api);
vi.mock("../../dev/ModeContext", () => ({ useMode: () => ({ isDev: false }) }));

const blocked: ReadinessRead = {
  play_allowed: false,
  history_allowed: true,
  state: "adventure_review_needed",
  primary_action: "review_adventure",
  blockers: [{
    code: "adventure_review_needed",
    message: "Review and publish the adventure before starting or continuing play.",
    action: "review_adventure",
  }],
};

function session(id: number, hasHistory: boolean): SessionRead {
  return {
    id,
    campaign_id: 2,
    started_at: `2026-10-1${id}T10:00:00Z`,
    ended_at: null,
    current_scene_id: null,
    active_character_id: null,
    has_history: hasHistory,
    last_played_at: hasHistory ? `2026-10-1${id}T11:00:00Z` : null,
    readiness: blocked,
  };
}

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.clearAllMocks();
  api.listSessions.mockResolvedValue([session(1, true), session(2, false)]);
  api.listCharacters.mockResolvedValue([]);
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(() => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
});

it("keeps readable blocked history available while hiding session creation and empty saves", async () => {
  await act(async () => {
    root.render(
      <MemoryRouter>
        <SessionsTab campaignId={2} ended={false} playAllowed readiness={blocked} />
      </MemoryRouter>,
    );
    await Promise.resolve();
  });

  expect(container.textContent).toContain("Read story");
  expect(container.textContent).not.toContain("Start a new session");
  expect(container.textContent).not.toContain("Start without a character");
  expect(Array.from(container.querySelectorAll("button")).some((button) => button.textContent?.trim() === "Continue")).toBe(false);
  expect(Array.from(container.querySelectorAll("button")).some((button) => button.textContent?.trim() === "Read story")).toBe(true);
  const storyButton = Array.from(container.querySelectorAll("button")).find((button) => button.textContent?.includes("Read story"));
  await act(() => storyButton?.click());
  expect(api.createSession).not.toHaveBeenCalled();
});
