import type {
  AdventureGenerateRequest,
  AdventureGenerateResponse,
  AdventureRead,
  CampaignCharacterRead,
  CampaignRead,
  CharacterListItem,
  CharacterPreset,
  CharacterRead,
  NPCRead,
  SessionRead,
  UserRead,
  WorldFlagRead,
} from "./types";

/** Relative to site origin; empty uses same origin (Vite proxy in dev, API container in prod). */
export const apiBase = import.meta.env.VITE_API_BASE_URL ?? "";

/** Shown whenever the browser cannot talk to the API (server down, proxy 502, offline). */
export const OFFLINE_MESSAGE = "Can't reach the Edgar server. Is it running?";

/** Pull FastAPI's `{"detail": "..."}` string out of a response body, if present. */
function parseDetail(body: string): string | null {
  try {
    const parsed = JSON.parse(body) as { detail?: unknown };
    if (parsed && typeof parsed.detail === "string" && parsed.detail.trim()) return parsed.detail;
  } catch {
    // body wasn't JSON
  }
  return null;
}

/** 502/503/504 with no FastAPI detail = the dev proxy / gateway couldn't reach the API. */
function isGatewayFailure(status: number, body: string): boolean {
  return (status === 502 || status === 503 || status === 504) && parseDetail(body) == null;
}

function friendlyMessage(status: number, body: string): string {
  if (status === 0 || isGatewayFailure(status, body)) return OFFLINE_MESSAGE;
  const detail = parseDetail(body);
  if (status >= 400 && status < 500) {
    if (detail) return detail;
    if (status === 404) return "That item no longer exists.";
    if (status === 409) return "That conflicts with the current state. Refresh and try again.";
    return "The request was rejected. Please check your input and try again.";
  }
  return "Something went wrong on the server. Please try again.";
}

/**
 * Error thrown by `apiFetch`. `message` is safe to show to players; `body`/`status`/`requestId`
 * carry the raw details (show those only in Developer mode — see `errorDetails`).
 * `status === 0` means the request never reached the server (network error).
 */
export class ApiError extends Error {
  readonly name = "ApiError";
  readonly status: number;
  readonly body: string;
  readonly requestId?: string;

  constructor(status: number, body: string, requestId?: string) {
    super(friendlyMessage(status, body));
    this.status = status;
    this.body = body;
    this.requestId = requestId;
  }

  /** True when the API is unreachable (network failure or gateway error). */
  get isOffline(): boolean {
    return this.status === 0 || isGatewayFailure(this.status, this.body);
  }
}

export async function apiFetch(path: string, init?: RequestInit): Promise<Response> {
  const url = path.startsWith("http") ? path : `${apiBase}${path}`;
  let res: Response;
  try {
    res = await fetch(url, {
      ...init,
      headers: {
        ...(init?.headers as Record<string, string> | undefined),
      },
    });
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

/** Player-facing message for any error (never raw response bodies). */
export function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return err.message;
  if (err instanceof TypeError) return OFFLINE_MESSAGE;
  if (err instanceof Error && err.message) return err.message;
  return "Something went wrong. Please try again.";
}

/** Raw technical details for Developer mode (status, request id, response body). */
export function errorDetails(err: unknown): string | null {
  if (err instanceof ApiError) {
    const head = err.status === 0 ? "Network error" : `HTTP ${err.status}`;
    const rid = err.requestId ? ` · request ${err.requestId}` : "";
    return `${head}${rid}${err.body ? `\n${err.body}` : ""}`;
  }
  if (err instanceof Error) return err.stack ?? err.message;
  return err == null ? null : String(err);
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

function sendJson<T>(method: string, path: string, body?: unknown): Promise<T> {
  return apiFetch(path, {
    method,
    headers: body === undefined ? undefined : JSON_HEADERS,
    body: body === undefined ? undefined : JSON.stringify(body),
  }).then((r) => json<T>(r));
}

async function send(method: string, path: string, body?: unknown): Promise<void> {
  await apiFetch(path, {
    method,
    headers: body === undefined ? undefined : JSON_HEADERS,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
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

export async function uploadAdventure(file: File, title?: string): Promise<AdventureRead> {
  const form = new FormData();
  form.append("file", file);
  if (title && title.trim()) form.append("title", title.trim());
  return json(await apiFetch("/api/adventures/upload", { method: "POST", body: form }));
}

/** Kick off background AI authoring of a campaign. Returns 202 with a processing stub. */
export function generateAdventure(
  body: AdventureGenerateRequest,
): Promise<AdventureGenerateResponse> {
  return sendJson("POST", "/api/adventures/generate", body);
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

// ── Campaigns ───────────────────────────────────────────────────────────────────

export function listCampaigns(signal?: AbortSignal): Promise<CampaignRead[]> {
  return getJson("/api/campaigns", signal);
}

export function getCampaign(id: number, signal?: AbortSignal): Promise<CampaignRead> {
  return getJson(`/api/campaigns/${id}`, signal);
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

export function listCampaignCharacters(
  campaignId: number,
  signal?: AbortSignal,
): Promise<CampaignCharacterRead[]> {
  return getJson(`/api/campaigns/${campaignId}/characters`, signal);
}

// ── NPCs & world flags ──────────────────────────────────────────────────────────

export function listNpcs(campaignId: number, signal?: AbortSignal): Promise<NPCRead[]> {
  return getJson(`/api/campaigns/${campaignId}/npcs`, signal);
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

export function createCharacter(body: CharacterCreateBody): Promise<CharacterRead> {
  return sendJson("POST", "/api/characters", body);
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

export function createSession(
  campaignId: number,
  activeCharacterId?: number,
): Promise<SessionRead> {
  return sendJson("POST", "/api/sessions", {
    campaign_id: campaignId,
    ...(activeCharacterId != null ? { active_character_id: activeCharacterId } : {}),
  });
}

export function deleteSession(id: number): Promise<void> {
  return send("DELETE", `/api/sessions/${id}`);
}

export function assignCharacterToCampaign(campaignId: number, characterId: number): Promise<void> {
  return send("POST", `/api/campaigns/${campaignId}/characters`, { character_id: characterId });
}
