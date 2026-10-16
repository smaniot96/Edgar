import type { CharacterCreateBody } from "../api/client";

export type CampaignSetupStage = "hero" | "assignment" | "playthrough" | "complete";

export interface CharacterDraft {
  name: string;
  presetKey: string | null;
  characterClass: string | null;
  hpText: string | null;
}

export interface PendingCharacterCreate {
  requestId: string;
  body: CharacterCreateBody;
}

/**
 * Progress is local to this campaign and contains only IDs and the player's own draft. The
 * backend remains authoritative; the IDs let a resumed setup reuse successful steps.
 */
export interface CampaignSetupProgress {
  version: 1;
  campaignId: number;
  stage: CampaignSetupStage;
  characterId: number | null;
  assignedCharacterId: number | null;
  sessionId: number | null;
  pendingCharacter: PendingCharacterCreate | null;
  pendingSession: { requestId: string; characterId: number | null } | null;
  characterDraft: CharacterDraft;
}

export const emptyCharacterDraft: CharacterDraft = {
  name: "",
  presetKey: null,
  characterClass: null,
  hpText: null,
};

export function newCampaignSetupProgress(campaignId: number): CampaignSetupProgress {
  return {
    version: 1,
    campaignId,
    stage: "hero",
    characterId: null,
    assignedCharacterId: null,
    sessionId: null,
    pendingCharacter: null,
    pendingSession: null,
    characterDraft: emptyCharacterDraft,
  };
}

export function campaignSetupStorageKey(campaignId: number): string {
  return `edgar:campaign-setup:v1:${campaignId}`;
}

function isPositiveId(value: unknown): value is number {
  return Number.isInteger(value) && Number(value) > 0;
}

function isProgress(value: unknown, campaignId: number): value is CampaignSetupProgress {
  if (!value || typeof value !== "object") return false;
  const progress = value as Partial<CampaignSetupProgress>;
  return progress.version === 1 && progress.campaignId === campaignId &&
    ["hero", "assignment", "playthrough", "complete"].includes(progress.stage ?? "") &&
    (progress.characterId == null || isPositiveId(progress.characterId)) &&
    (progress.assignedCharacterId == null || isPositiveId(progress.assignedCharacterId)) &&
    (progress.sessionId == null || isPositiveId(progress.sessionId));
}

export function readCampaignSetupProgress(campaignId: number): CampaignSetupProgress {
  try {
    const raw = window.localStorage.getItem(campaignSetupStorageKey(campaignId));
    if (!raw) return newCampaignSetupProgress(campaignId);
    const value: unknown = JSON.parse(raw);
    return isProgress(value, campaignId) ? value : newCampaignSetupProgress(campaignId);
  } catch {
    return newCampaignSetupProgress(campaignId);
  }
}

export function writeCampaignSetupProgress(progress: CampaignSetupProgress): void {
  try {
    window.localStorage.setItem(campaignSetupStorageKey(progress.campaignId), JSON.stringify(progress));
  } catch {
    // Private browsing or a full storage quota should not prevent a player from continuing.
  }
}

export function clearCampaignSetupProgress(campaignId: number): void {
  try {
    window.localStorage.removeItem(campaignSetupStorageKey(campaignId));
  } catch {
    // Storage is optional; the campaign and session records remain on the server.
  }
}

export function createRequestId(): string {
  return crypto.randomUUID();
}
