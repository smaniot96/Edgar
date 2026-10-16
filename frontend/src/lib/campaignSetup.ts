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

// Keep the player's progress alive across in-app navigation when browser storage is blocked
// (for example, in a private session with storage disabled). A reload still needs localStorage.
const inMemoryProgress = new Map<number, CampaignSetupProgress>();
const failedStorageWrites = new Map<number, string | null | undefined>();

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

function isNullablePositiveId(value: unknown): value is number | null {
  return value === null || isPositiveId(value);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isRequestId(value: unknown): value is string {
  return typeof value === "string" && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value);
}

function isCharacterDraft(value: unknown): value is CharacterDraft {
  if (!isRecord(value)) return false;
  return typeof value.name === "string" && value.name.length <= 255 &&
    (value.presetKey === null || (typeof value.presetKey === "string" && value.presetKey.length <= 255)) &&
    (value.characterClass === null || (typeof value.characterClass === "string" && value.characterClass.length <= 255)) &&
    (value.hpText === null || (typeof value.hpText === "string" && value.hpText.length <= 32));
}

function isCharacterCreateBody(value: unknown): value is CharacterCreateBody {
  if (!isRecord(value)) return false;
  return typeof value.name === "string" && value.name.trim().length > 0 && value.name.length <= 255 &&
    typeof value.character_class === "string" && value.character_class.trim().length > 0 && value.character_class.length <= 255 &&
    Number.isInteger(value.level) && Number(value.level) >= 1 && Number(value.level) <= 20 &&
    Number.isInteger(value.hp_max) && Number(value.hp_max) >= 1 &&
    isRecord(value.base_stats) && isRecord(value.base_inventory);
}

function isPendingCharacter(value: unknown): value is PendingCharacterCreate | null {
  return value === null || (isRecord(value) && isRequestId(value.requestId) && isCharacterCreateBody(value.body));
}

function isPendingSession(value: unknown): value is CampaignSetupProgress["pendingSession"] {
  return value === null || (isRecord(value) && isRequestId(value.requestId) && isNullablePositiveId(value.characterId));
}

function isProgress(value: unknown, campaignId: number): value is CampaignSetupProgress {
  if (!value || typeof value !== "object") return false;
  const progress = value as Partial<CampaignSetupProgress>;
  return progress.version === 1 && progress.campaignId === campaignId &&
    ["hero", "assignment", "playthrough", "complete"].includes(progress.stage ?? "") &&
    isNullablePositiveId(progress.characterId) &&
    isNullablePositiveId(progress.assignedCharacterId) &&
    isNullablePositiveId(progress.sessionId) &&
    isPendingCharacter(progress.pendingCharacter) && isPendingSession(progress.pendingSession) &&
    isCharacterDraft(progress.characterDraft);
}

export function readCampaignSetupProgress(campaignId: number): CampaignSetupProgress {
  try {
    const raw = window.localStorage.getItem(campaignSetupStorageKey(campaignId));
    if (failedStorageWrites.has(campaignId) && failedStorageWrites.get(campaignId) === raw) {
      return inMemoryProgress.get(campaignId) ?? newCampaignSetupProgress(campaignId);
    }
    if (!raw) return inMemoryProgress.get(campaignId) ?? newCampaignSetupProgress(campaignId);
    const value: unknown = JSON.parse(raw);
    if (isProgress(value, campaignId)) {
      failedStorageWrites.delete(campaignId);
      inMemoryProgress.set(campaignId, value);
      return value;
    }
    return inMemoryProgress.get(campaignId) ?? newCampaignSetupProgress(campaignId);
  } catch {
    return inMemoryProgress.get(campaignId) ?? newCampaignSetupProgress(campaignId);
  }
}

export function writeCampaignSetupProgress(progress: CampaignSetupProgress): boolean {
  inMemoryProgress.set(progress.campaignId, progress);
  try {
    window.localStorage.setItem(campaignSetupStorageKey(progress.campaignId), JSON.stringify(progress));
    failedStorageWrites.delete(progress.campaignId);
    return true;
  } catch {
    let previous: string | null | undefined;
    try { previous = window.localStorage.getItem(campaignSetupStorageKey(progress.campaignId)); } catch { /* Read access may also be blocked. */ }
    failedStorageWrites.set(progress.campaignId, previous);
    return false;
  }
}

export function clearCampaignSetupProgress(campaignId: number): void {
  inMemoryProgress.delete(campaignId);
  failedStorageWrites.delete(campaignId);
  try {
    window.localStorage.removeItem(campaignSetupStorageKey(campaignId));
  } catch {
    // Storage is optional; the campaign and session records remain on the server.
  }
}

export function createRequestId(): string {
  return crypto.randomUUID();
}
