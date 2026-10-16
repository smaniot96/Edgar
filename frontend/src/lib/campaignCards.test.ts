import { expect, it } from "vitest";

import type { ReadinessRead, SessionRead } from "../api/types";
import { selectResumeSession } from "./campaignCards";

const ready: ReadinessRead = {
  play_allowed: true,
  history_allowed: true,
  state: "ready",
  primary_action: "play",
  blockers: [],
};
const blocked: ReadinessRead = {
  play_allowed: false,
  history_allowed: true,
  state: "source_missing",
  primary_action: "recover_source",
  blockers: [{ code: "source_missing", message: "The source is missing.", action: "recover_source" }],
};

function session(id: number, overrides: Partial<SessionRead> = {}): SessionRead {
  return {
    id,
    campaign_id: 1,
    started_at: `2026-10-${String(id).padStart(2, "0")}T10:00:00Z`,
    ended_at: null,
    current_scene_id: null,
    active_character_id: 3,
    has_history: true,
    last_played_at: `2026-10-${String(id).padStart(2, "0")}T10:00:00Z`,
    readiness: ready,
    ...overrides,
  };
}

it("resumes the newest playable save with history over a newer blocked or empty save", () => {
  const chosen = selectResumeSession([
    session(13, { readiness: blocked, last_played_at: "2026-10-15T12:00:00Z" }),
    session(12, { has_history: false, readiness: ready, last_played_at: null }),
    session(10, { last_played_at: "2026-10-14T12:00:00Z" }),
    session(11, { last_played_at: "2026-10-15T09:00:00Z" }),
  ]);
  expect(chosen?.id).toBe(11);
});

it("falls back to the newest readable history if every saved session is blocked", () => {
  const chosen = selectResumeSession([
    session(1, { readiness: blocked }),
    session(2, { readiness: { ...blocked, history_allowed: false } }),
  ]);
  expect(chosen?.id).toBe(1);
  expect(selectResumeSession([session(3, { has_history: false })])).toBeNull();
});
