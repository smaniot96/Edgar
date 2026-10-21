import { authorizedFetch } from "./auth";
import type { SpotlightMode, SpotlightState } from "./sessionChannel";
import { safeTurnError, GENERIC_SAFE_ERROR, type TurnError } from "./turnStream";

import type {
  AdventureGenerateRequest,
  AdventureGenerateResponse,
  AdventureRead,
  AdventureJobRead,
  ExtractionPreview,
  ManifestInspection,
  SoloAdventureReview,
  CampaignCharacterRead,
  CampaignDeleteRead,
  CampaignRead,
  CharacterAssignmentHistoryRead,
  CharacterListItem,
  CharacterPreset,
  CharacterRead,
  ChatMessageRead,
  CombatState,
  InviteAccepted,
  InviteCreated,
  InviteRead,
  InviteRole,
  MemberRead,
  MemberRole,
  NPCRead,
  NPCListRead,
  SessionRead,
  SessionIntroRead,
  SessionLobby,
  SessionMapRead,
  UserRead,
  WorldFlagRead,
} from "./types";

/** Relative to site origin; empty uses same origin (Vite proxy in dev, API container in prod). */
export const apiBase = import.meta.env.VITE_API_BASE_URL ?? "";

export function getExtractionPreview(slug: string): Promise<ExtractionPreview> {
  return getJson(`/api/adventures/${encodeURIComponent(slug)}/extraction`);
}

/** Shown whenever the browser cannot talk to the API (server down, proxy 502, offline). */
export const OFFLINE_MESSAGE = "Can't reach the Edgar server. Is it running?";

/** A gateway response without a recognized structured failure has unknown outcome. */
function isGatewayFailure(status: number, body: string): boolean {
  if (![502, 503, 504].includes(status)) return false;
  try {
    const parsed = JSON.parse(body) as Record<string, unknown>;
    return !parsed || typeof parsed !== "object";
  } catch { return true; }
}

function fallbackCode(status: number): string {
  if (status === 401) return "authentication_required";
  if (status === 403) return "forbidden";
  if (status === 404) return "not_found";
  if (status === 400 || status === 422) return "validation_failed";
  if (status === 409) return "busy";
  if (status === 429) return "rate_limited";
  if (status === 0 || [502, 503, 504].includes(status)) return "interrupted";
  return "turn_failed";
}

/** Error state contains only the bounded public contract, never the raw response body. */
export class ApiError extends Error {
  readonly name = "ApiError";
  readonly status: number;
  readonly body: string;
  readonly requestId?: string;
  readonly failure: TurnError;
  private readonly offline: boolean;

  constructor(status: number, body: string, requestId?: string) {
    let failure = safeTurnError(body);
    let explicitInternal = false;
    try {
      const parsed = JSON.parse(body) as { code?: unknown; detail?: { code?: unknown } };
      explicitInternal = parsed.code === "turn_failed" || parsed.detail?.code === "turn_failed";
    } catch { /* An unstructured gateway response has no classified code. */ }
    if (failure.code === "turn_failed" && !failure.supportRef && !explicitInternal) {
      failure = safeTurnError({ code: fallbackCode(status) });
    }
    const offline = status === 0 || isGatewayFailure(status, body);
    super(offline ? OFFLINE_MESSAGE : failure.message);
    this.status = status;
    this.failure = failure;
    this.offline = offline;
    this.body = JSON.stringify({
      code: failure.code, category: failure.category, detail: failure.message,
      message: failure.message, recovery_action: failure.recoveryAction,
      retryable: failure.retryable, outcome: failure.outcome, support_ref: failure.supportRef,
      retry_after_seconds: failure.retryAfterSeconds,
      daily_budget: failure.dailyBudget ? {
        requested_microusd: failure.dailyBudget.requestedMicrousd,
        remaining_microusd: failure.dailyBudget.remainingMicrousd,
      } : undefined,
    });
    this.requestId = requestId && /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(requestId)
      ? requestId : undefined;
  }

  get isOffline(): boolean { return this.offline; }
}

/** Authorized request to a final URL. Every API call in the client ends up here (EDG-160). */
async function requestOk(url: string, init?: RequestInit): Promise<Response> {
  let res: Response;
  try {
    res = await authorizedFetch(url, init);
  } catch (err) {
    // Let aborts propagate untouched so callers can ignore them.
    if (err instanceof DOMException && err.name === "AbortError") throw err;
    throw new ApiError(0, err instanceof Error ? err.message : String(err));
  }
  const requestId = res.headers.get("x-request-id") ?? undefined;
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new ApiError(res.status, text, requestId);
  }
  return res;
}

export function apiFetch(path: string, init?: RequestInit): Promise<Response> {
  return requestOk(path.startsWith("http") ? path : `${apiBase}${path}`, init);
}

/**
 * Binary or resource GET (images, downloads) as a Blob. Browser-navigated URLs cannot carry an
 * Authorization header, so these are fetched here and shown or saved from an object URL.
 */
export async function fetchBlob(url: string, signal?: AbortSignal): Promise<Blob> {
  return (await requestOk(url, { signal })).blob();
}

/** Player-facing fixed copy; arbitrary Error messages can contain private input. */
export function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return err.message;
  if (err instanceof TypeError) return OFFLINE_MESSAGE;
  return GENERIC_SAFE_ERROR;
}

/** Bounded diagnostics are safe in both Player and Developer mode. */
export function errorDetails(err: unknown): string | null {
  if (!(err instanceof ApiError)) return null;
  const failure = err.failure;
  return [
    err.status === 0 ? "Network error" : `HTTP ${err.status}`,
    err.requestId ? `Request: ${err.requestId}` : null,
    failure.code ? `Code: ${failure.code}` : null,
    failure.category ? `Category: ${failure.category}` : null,
    failure.outcome ? `Outcome: ${failure.outcome}` : null,
    failure.supportRef ? `Reference: ${failure.supportRef}` : null,
  ].filter(Boolean).join("\n");
}

/** True when the error means the API could not be reached at all. */
export function isOfflineError(err: unknown): boolean {
  return (err instanceof ApiError && err.isOffline) || err instanceof TypeError;
}

/** True when the error is an HTTP 404. */
export function isNotFound(err: unknown): boolean {
  return err instanceof ApiError && err.status === 404;
}

const JSON_HEADERS = { "content-type": "application/json" };

async function json<T>(res: Response): Promise<T> {
  return (await res.json()) as T;
}

function getJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  return apiFetch(path, { signal }).then((r) => json<T>(r));
}

function sendRequest(method: string, path: string, body?: unknown): Promise<Response> {
  return apiFetch(path, {
    method,
    headers: body === undefined ? undefined : JSON_HEADERS,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

function sendJson<T>(method: string, path: string, body?: unknown): Promise<T> {
  return sendRequest(method, path, body).then((r) => json<T>(r));
}

async function send(method: string, path: string, body?: unknown): Promise<void> {
  await sendRequest(method, path, body);
}

// ── Users ───────────────────────────────────────────────────────────────────────

/** Current user, or `null` when no user exists yet (first run). */
export async function getMe(signal?: AbortSignal): Promise<UserRead | null> {
  try {
    return await getJson<UserRead>("/api/users/me", signal);
  } catch (err) {
    if (isNotFound(err)) return null;
    throw err;
  }
}

export function createUser(body: { email: string; display_name: string | null }): Promise<UserRead> {
  return sendJson("POST", "/api/users", body);
}

export function updateMe(body: { display_name: string | null }): Promise<UserRead> {
  return sendJson("PATCH", "/api/users/me", body);
}

// ── Adventures (= campaign modules) ─────────────────────────────────────────────

export function listAdventures(signal?: AbortSignal): Promise<AdventureRead[]> {
  return getJson("/api/adventures", signal);
}

export function getAdventure(slug: string, signal?: AbortSignal): Promise<AdventureRead> {
  return getJson(`/api/adventures/${encodeURIComponent(slug)}`, signal);
}

export function setAdventureLibraryArchived(slug: string, archived: boolean): Promise<AdventureRead> {
  return sendJson("PATCH", `/api/adventures/${encodeURIComponent(slug)}/library`, { archived });
}

export async function uploadAdventure(file: File, title?: string, rulesEdition: "2014" | "2024" | "unspecified" = "unspecified", preparation?: import("./types").PreparationSetup): Promise<AdventureRead> {
  const form = new FormData();
  form.append("file", file);
  form.append("rules_edition", rulesEdition);
  if (preparation) form.append("preparation", JSON.stringify(preparation));
  if (title && title.trim()) form.append("title", title.trim());
  return json(await apiFetch("/api/adventures/upload", { method: "POST", body: form }));
}

/** Kick off background AI authoring of a campaign. Returns 202 with a processing stub. */
export function generateAdventure(
  body: AdventureGenerateRequest,
): Promise<AdventureGenerateResponse> {
  return sendJson("POST", "/api/adventures/generate", body);
}

export function completePreparationSetup(slug: string, rules_edition: "2014" | "2024", preparation: import("./types").PreparationSetup): Promise<AdventureGenerateResponse> {
  return sendJson("POST", `/api/adventures/${encodeURIComponent(slug)}/preparation/setup`, { rules_edition, preparation });
}

export async function deleteAdventure(slug: string): Promise<void> {
  await apiFetch(`/api/adventures/${encodeURIComponent(slug)}`, {
    method: "DELETE",
    headers: { "X-Confirm-Delete": "yes" },
  });
}

/** Get-or-create the playable campaign row for an adventure module. */
export function getOrCreateCampaign(slug: string): Promise<CampaignRead> {
  return sendJson("POST", `/api/adventures/${encodeURIComponent(slug)}/campaign`);
}

/** Start an independent playthrough; reuse the request ID when retrying an ambiguous response. */
export function startAdventureCampaign(slug: string, requestId: string): Promise<CampaignRead> {
  return sendJson("POST", `/api/adventures/${encodeURIComponent(slug)}/campaign`, {
    new_campaign: true, request_id: requestId,
  });
}

// ── Campaigns ───────────────────────────────────────────────────────────────────

export function listCampaigns(signal?: AbortSignal): Promise<CampaignRead[]> {
  return getJson("/api/campaigns", signal);
}

export function getCampaign(id: number, signal?: AbortSignal): Promise<CampaignRead> {
  return getJson(`/api/campaigns/${id}`, signal);
}

export function setCampaignRulesEdition(id: number, rules_edition: "2014" | "2024"): Promise<CampaignRead> {
  return sendJson("PATCH", `/api/campaigns/${id}`, { rules_edition });
}

export function setCampaignSourceMode(id: number, source_mode: "module" | "homebrew"): Promise<CampaignRead> {
  return sendJson("PATCH", `/api/campaigns/${id}`, { source_mode });
}

/** EDG-045: 'milestone' grants milestones on won encounters, spent by an explicit level-up. */
export function setCampaignProgressionMode(id: number, progression_mode: "fixed" | "milestone"): Promise<CampaignRead> {
  return sendJson("PATCH", `/api/campaigns/${id}`, { progression_mode });
}

/** EDG-045: 'death_saves' applies the death saving throw rules; 'subdued' keeps the 1 HP continuation. */
export function setCampaignDeathMode(id: number, death_mode: "subdued" | "death_saves"): Promise<CampaignRead> {
  return sendJson("PATCH", `/api/campaigns/${id}`, { death_mode });
}

/** EDG-182: confirm every combat turn that declares an action, for every hero in the campaign. */
export function setCampaignCombatAlwaysConfirm(id: number, combat_always_confirm: boolean): Promise<CampaignRead> {
  return sendJson("PATCH", `/api/campaigns/${id}`, { combat_always_confirm });
}

/** EDG-045: spend one milestone on one level. The request names the level it starts from. */
export function levelUpCampaignCharacter(
  campaignId: number,
  characterId: number,
  body: import("./types").LevelUpRequestBody,
): Promise<import("./types").LevelUpRead> {
  return sendJson("POST", `/api/campaigns/${campaignId}/characters/${characterId}/level-up`, body);
}

export function retryAdventure(slug: string, reviewed?: { reviewed_source_sha256: string; reviewed_extraction_sha256: string }): Promise<AdventureRead> {
  return sendJson("POST", `/api/adventures/${encodeURIComponent(slug)}/retry`, reviewed);
}

/** Host review of a legacy adventure: inspect the current draft (409 when none is compiled). */
export function inspectAdventureManifest(slug: string, signal?: AbortSignal): Promise<ManifestInspection> {
  return getJson<ManifestInspection>(`/api/adventures/${encodeURIComponent(slug)}/manifest`, signal);
}

/** Solo host's spoiler-safe review for one campaign; shared campaigns return only the mode. */
export function getSoloAdventureReview(slug: string, campaignId: number, signal?: AbortSignal): Promise<SoloAdventureReview> {
  return getJson<SoloAdventureReview>(`/api/adventures/${encodeURIComponent(slug)}/manifest/solo-review?campaign_id=${campaignId}`, signal);
}

/** Records that a solo host opened the passage-level review. It changes no publication state. */
export function recordSoloSpoilerReview(slug: string, campaignId: number): Promise<{ recorded: boolean }> {
  return sendJson("POST", `/api/adventures/${encodeURIComponent(slug)}/manifest/solo-review/spoiler-opened?campaign_id=${campaignId}`, {});
}

export function compileAdventureManifest(slug: string): Promise<ManifestInspection> {
  return sendJson("POST", `/api/adventures/${encodeURIComponent(slug)}/manifest/compile`);
}

export function saveAdventureManifest(slug: string, manifest: Record<string, unknown>): Promise<ManifestInspection> {
  return sendJson("PUT", `/api/adventures/${encodeURIComponent(slug)}/manifest`, manifest);
}

export function publishAdventureManifest(slug: string, reviewed_digest: string, reviewer: string): Promise<{ status: string; chunks: number }> {
  return sendJson("POST", `/api/adventures/${encodeURIComponent(slug)}/manifest/publish`, { reviewed_digest, reviewer });
}

export function cancelAdventure(slug: string): Promise<AdventureJobRead> {
  return sendJson("POST", `/api/adventures/${encodeURIComponent(slug)}/cancel`);
}

export function endCampaign(id: number): Promise<CampaignRead> {
  return sendJson("POST", `/api/campaigns/${id}/end`);
}

export function reopenCampaign(id: number): Promise<CampaignRead> {
  return sendJson("POST", `/api/campaigns/${id}/reopen`);
}

export function deleteCampaign(id: number): Promise<void> {
  return send("DELETE", `/api/campaigns/${id}`);
}

/**
 * Delete a campaign and its saves, then each bound adventure source that no other campaign uses
 * and that may still change (EDG-055 F16). The outcome says what happened to every source.
 */
export async function deleteCampaignAndUnusedSources(id: number): Promise<CampaignDeleteRead> {
  return json(await apiFetch(`/api/campaigns/${id}?remove_unused_sources=true`, {
    method: "DELETE",
    headers: { "X-Confirm-Delete": "yes" },
  }));
}

export function listCampaignCharacters(
  campaignId: number,
  signal?: AbortSignal,
): Promise<CampaignCharacterRead[]> {
  return getJson(`/api/campaigns/${campaignId}/characters`, signal);
}

// ── NPCs & world flags ──────────────────────────────────────────────────────────

export async function listNpcs(
  campaignId: number,
  sessionId?: number | null,
  signal?: AbortSignal,
): Promise<NPCListRead> {
  const query = sessionId == null ? "" : `?session_id=${sessionId}`;
  const response = await apiFetch(`/api/campaigns/${campaignId}/npcs${query}`, { signal });
  const tracking = response.headers.get("x-npc-tracking");
  return {
    npcs: (await response.json()) as NPCRead[],
    tracking:
      tracking === "legacy_untracked" ? "legacy_untracked" : sessionId == null ? "not_selected" : "available",
  };
}

export function createNpc(
  campaignId: number,
  body: { name: string; disposition: string; stat_block?: Record<string, unknown> },
): Promise<NPCRead> {
  return sendJson("POST", `/api/campaigns/${campaignId}/npcs`, { stat_block: {}, ...body });
}

export function deleteNpc(id: number): Promise<void> {
  return send("DELETE", `/api/npcs/${id}`);
}

export function updateNpc(
  id: number,
  body: { name: string; disposition: string },
): Promise<NPCRead> {
  return sendJson("PATCH", `/api/npcs/${id}`, body);
}

export function listWorldFlags(campaignId: number, signal?: AbortSignal): Promise<WorldFlagRead[]> {
  return getJson(`/api/campaigns/${campaignId}/world-flags`, signal);
}

export function createWorldFlag(
  campaignId: number,
  body: { key: string; value: string },
): Promise<WorldFlagRead> {
  return sendJson("POST", `/api/campaigns/${campaignId}/world-flags`, body);
}

export function updateWorldFlag(
  campaignId: number,
  key: string,
  value: string,
): Promise<WorldFlagRead> {
  return sendJson(
    "PATCH",
    `/api/campaigns/${campaignId}/world-flags/${encodeURIComponent(key)}`,
    { value },
  );
}

export function deleteWorldFlag(campaignId: number, key: string): Promise<void> {
  return send("DELETE", `/api/campaigns/${campaignId}/world-flags/${encodeURIComponent(key)}`);
}

// ── Characters (PG) ─────────────────────────────────────────────────────────────

export function listCharacters(signal?: AbortSignal): Promise<CharacterListItem[]> {
  return getJson("/api/characters", signal);
}

export function getCharacter(id: number, signal?: AbortSignal): Promise<CharacterRead> {
  return getJson(`/api/characters/${id}`, signal);
}

export function listCharacterAssignments(
  id: number,
  signal?: AbortSignal,
): Promise<CharacterAssignmentHistoryRead[]> {
  return getJson(`/api/characters/${id}/assignments`, signal);
}

export function listCharacterPresets(signal?: AbortSignal): Promise<CharacterPreset[]> {
  return getJson("/api/character-presets", signal);
}

export interface CharacterCreateBody {
  name: string;
  character_class: string;
  level: number;
  hp_max: number;
  base_stats: Record<string, unknown>;
  base_inventory: Record<string, unknown>;
}

export function createCharacter(body: CharacterCreateBody, requestId?: string): Promise<CharacterRead> {
  return sendJson("POST", "/api/characters", { ...body, ...(requestId ? { request_id: requestId } : {}) });
}

export function deleteCharacter(id: number): Promise<void> {
  return send("DELETE", `/api/characters/${id}`);
}

// ── Sessions & campaign assignment ──────────────────────────────────────────────

/** All sessions, or only those of one campaign. */
export function listSessions(campaignId?: number, signal?: AbortSignal): Promise<SessionRead[]> {
  const q = campaignId != null ? `?campaign_id=${campaignId}` : "";
  return getJson(`/api/sessions${q}`, signal);
}

export function getSession(id: number | string, signal?: AbortSignal): Promise<SessionRead> {
  return getJson(`/api/sessions/${id}`, signal);
}

export function getSessionMap(id: number | string, signal?: AbortSignal): Promise<SessionMapRead> {
  return getJson(`/api/sessions/${id}/map`, signal);
}

export function listSessionMessages(id: number, signal?: AbortSignal): Promise<ChatMessageRead[]> {
  return getJson(`/api/sessions/${id}/messages`, signal);
}

/** Developer only (EDG-138): stored traces of captured DM turns, oldest first. 404 when debug is off. */
export function listSessionTurnTraces(id: number, signal?: AbortSignal): Promise<import("./types").TurnTraceList> {
  return getJson(`/api/sessions/${id}/diagnostics/turn-traces`, signal);
}

export function getSessionCombat(id: number, signal?: AbortSignal): Promise<CombatState | null> {
  return getJson(`/api/sessions/${id}/combat`, signal);
}

export function startSessionIntro(id: number): Promise<SessionIntroRead> {
  return sendJson("POST", `/api/sessions/${id}/intro`);
}

export function createSession(
  campaignId: number,
  activeCharacterId?: number,
  requestId?: string,
): Promise<SessionRead> {
  return sendJson("POST", "/api/sessions", {
    campaign_id: campaignId,
    ...(requestId ? { request_id: requestId } : {}),
    ...(activeCharacterId != null ? { active_character_id: activeCharacterId } : {}),
  });
}

export function deleteSession(id: number): Promise<void> {
  return send("DELETE", `/api/sessions/${id}`);
}

export function assignCharacterToCampaign(campaignId: number, characterId: number): Promise<void> {
  return send("POST", `/api/campaigns/${campaignId}/characters`, { character_id: characterId });
}

// ── Multiplayer table: lobby, members, invites, spotlight (EDG-046/048/050) ─────

/** Member-readable roster of a table: every role, spectators included. */
export function getSessionLobby(id: number, signal?: AbortSignal): Promise<SessionLobby> {
  return getJson(`/api/sessions/${id}/lobby`, signal);
}

export function getSpotlight(id: number, signal?: AbortSignal): Promise<SpotlightState> {
  return getJson(`/api/sessions/${id}/spotlight`, signal);
}

/** Owner only. Omit a field to leave it alone; `holder_character_id: null` releases the spotlight. */
export function updateSpotlight(
  id: number,
  body: {
    mode?: SpotlightMode;
    timeout_seconds?: number;
    holder_character_id?: number | null;
    expected_version?: number;
  },
): Promise<SpotlightState> {
  return sendJson("PATCH", `/api/sessions/${id}/spotlight`, body);
}

/** Best-effort attention report; a failure never matters to the player. */
export async function reportPresence(id: number, state: "active" | "idle"): Promise<void> {
  await send("POST", `/api/sessions/${id}/presence`, { state });
}

export function listMembers(campaignId: number, signal?: AbortSignal): Promise<MemberRead[]> {
  return getJson(`/api/campaigns/${campaignId}/members`, signal);
}

export function changeMemberRole(
  campaignId: number,
  userId: number,
  role: Exclude<MemberRole, "owner">,
): Promise<MemberRead> {
  return sendJson("PATCH", `/api/campaigns/${campaignId}/members/${userId}`, { role });
}

export function removeMember(campaignId: number, userId: number): Promise<void> {
  return send("DELETE", `/api/campaigns/${campaignId}/members/${userId}`);
}

export function leaveCampaign(campaignId: number): Promise<void> {
  return send("POST", `/api/campaigns/${campaignId}/leave`);
}

export function listInvites(campaignId: number, signal?: AbortSignal): Promise<InviteRead[]> {
  return getJson(`/api/campaigns/${campaignId}/invites`, signal);
}

export function createInvite(
  campaignId: number,
  body: { role: InviteRole; session_id?: number; expires_in_hours?: number },
): Promise<InviteCreated> {
  return sendJson("POST", `/api/campaigns/${campaignId}/invites`, body);
}

export function revokeInvite(campaignId: number, inviteId: number): Promise<void> {
  return send("DELETE", `/api/campaigns/${campaignId}/invites/${inviteId}`);
}

export function acceptInvite(token: string, characterId?: number): Promise<InviteAccepted> {
  return sendJson("POST", "/api/invites/accept", {
    token,
    ...(characterId != null ? { character_id: characterId } : {}),
  });
}
