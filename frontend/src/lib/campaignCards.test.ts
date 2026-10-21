import { expect, it } from "vitest";

import type { AdventureRead, CampaignRead, ReadinessRead, SessionRead } from "../api/types";
import { buildCards, selectCampaignSession, selectResumeSession } from "./campaignCards";

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

it("keeps a campaign's played save primary over newer blank, blocked or unrelated saves", () => {
  expect(selectCampaignSession([
    session(1), session(2, { has_history: false }), session(3, { readiness: blocked }),
    session(4, { campaign_id: 9 }),
  ], 1)?.id).toBe(1);
  expect(selectCampaignSession([session(1, { readiness: blocked }), session(2, { has_history: false })], 1)?.id).toBe(2);
  expect(selectCampaignSession([session(1, { readiness: blocked })], 1)?.id).toBe(1);
});

it("hides an archived duplicate even when saved campaigns would otherwise recreate its card", () => {
  const oldSource: AdventureRead = {
    slug: "sheep_old", title: "The Wild Sheep Chase", description: null,
    level_range: null, cover_image: null, status: "ready", library_archived: true,
  };
  const currentSource: AdventureRead = { ...oldSource, slug: "sheep_current", library_archived: false };
  const oldCampaign: CampaignRead = {
    id: 1, title: oldSource.title, system: "D&D 5e", created_by: 1,
    created_at: "2026-10-20T10:00:00Z", adventure_collections: [oldSource.slug],
    status: "active", ended_at: null,
  };
  const campaigns = [oldCampaign];
  const saved = [session(1)];
  const sources = [oldSource, currentSource];
  expect(buildCards(sources, campaigns, saved, 1).map((card) => card.adventure.slug)).toEqual(["sheep_current"]);
  const archived = buildCards(sources, campaigns, saved, 1, true).find((card) => card.adventure.slug === "sheep_old");
  expect(archived?.primarySession?.id).toBe(1);
  expect(archived?.campaign?.id).toBe(1);
  expect(oldSource.library_archived).toBe(true);
  expect(campaigns).toEqual([oldCampaign]);
  expect(saved).toEqual([session(1)]);
});

it("uses the reusable adventure title while retaining each independently named campaign", () => {
  const source: AdventureRead = {
    slug: "sheep", title: "The Wild Sheep Chase", description: null,
    level_range: null, cover_image: null, status: "ready",
  };
  const campaigns: CampaignRead[] = [1, 2].map((id) => ({
    id, title: `An independent journey ${id}`, system: "D&D 5e", created_by: 1,
    created_at: `2026-10-${20 + id}T10:00:00Z`, adventure_collections: [source.slug],
    status: "active", ended_at: null,
  }));
  const card = buildCards([source], campaigns, [], 1)[0];
  expect(card.name).toBe("The Wild Sheep Chase");
  expect(card.campaigns.map((campaign) => campaign.id)).toEqual([2, 1]);
  expect(campaigns.map((campaign) => campaign.title)).toEqual(["An independent journey 1", "An independent journey 2"]);
});
