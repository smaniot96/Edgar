// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it } from "vitest";

import {
  campaignSetupStorageKey,
  clearCampaignSetupProgress,
  newCampaignSetupProgress,
  readCampaignSetupProgress,
} from "./campaignSetup";

const campaignId = 12;

beforeEach(() => {
  window.localStorage.clear();
  clearCampaignSetupProgress(campaignId);
});

afterEach(() => {
  clearCampaignSetupProgress(campaignId);
});

it("uses fresh safe defaults when persisted setup data is missing required fields", () => {
  window.localStorage.setItem(campaignSetupStorageKey(campaignId), JSON.stringify({
    version: 1,
    campaignId,
    stage: "hero",
    characterId: null,
    assignedCharacterId: null,
    sessionId: null,
  }));

  expect(readCampaignSetupProgress(campaignId)).toEqual(newCampaignSetupProgress(campaignId));
});

it.each([
  { label: "pending hero request", field: "pendingCharacter", value: { requestId: "bad id", body: {} } },
  { label: "non-UUID retry key", field: "pendingSession", value: { requestId: "arbitrary-key", characterId: null } },
  { label: "pending playthrough request", field: "pendingSession", value: { requestId: 3, characterId: null } },
  { label: "hero draft", field: "characterDraft", value: { name: "Mara", presetKey: 7, characterClass: null, hpText: null } },
])("uses safe defaults when persisted $label is malformed", ({ field, value }) => {
  const progress = newCampaignSetupProgress(campaignId);
  window.localStorage.setItem(campaignSetupStorageKey(campaignId), JSON.stringify({ ...progress, [field]: value }));

  expect(readCampaignSetupProgress(campaignId)).toEqual(progress);
});

it("keeps required character and assignment IDs valid when reading a saved step", () => {
  const progress = {
    ...newCampaignSetupProgress(campaignId),
    stage: "playthrough" as const,
    characterId: 30,
    assignedCharacterId: 30,
    pendingSession: { requestId: "11111111-1111-4111-8111-111111111111", characterId: 30 },
  };
  window.localStorage.setItem(campaignSetupStorageKey(campaignId), JSON.stringify(progress));

  expect(readCampaignSetupProgress(campaignId)).toEqual(progress);
});
