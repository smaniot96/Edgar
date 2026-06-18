import type {
  AdventureGenerateRequest,
  AdventureGenerateResponse,
  AdventureRead,
  CampaignRead,
  CharacterListItem,
  CharacterPreset,
  CharacterRead,
  SessionRead,
} from "./types";

/** Relative to site origin; empty uses same origin (Vite proxy in dev, API container in prod). */
export const apiBase = import.meta.env.VITE_API_BASE_URL ?? "";

export class ApiError extends Error {
  readonly name = "ApiError";
  readonly status: number;
  readonly body: string;
  readonly requestId?: string;

  constructor(status: number, body: string, requestId?: string) {
    super(`API ${status}: ${body}`);
    this.status = status;
    this.body = body;
    this.requestId = requestId;
  }
}

export async function apiFetch(path: string, init?: RequestInit): Promise<Response> {
  const url = path.startsWith("http") ? path : `${apiBase}${path}`;
  const res = await fetch(url, {
    ...init,
    headers: {
      ...(init?.headers as Record<string, string> | undefined),
    },
  });
  const requestId = res.headers.get("x-request-id") ?? undefined;
  if (!res.ok) {
    const text = await res.text();
    throw new ApiError(res.status, text, requestId);
  }
  return res;
}

/** Extract a human-readable message from an unknown error (prefer ApiError.message). */
export function errorMessage(err: unknown): string {
  if (err instanceof ApiError) {
    // FastAPI often returns {"detail": "..."} — try to surface that cleanly.
    try {
      const parsed = JSON.parse(err.body) as { detail?: unknown };
      if (parsed && typeof parsed.detail === "string") return parsed.detail;
    } catch {
      // body wasn't JSON; fall through
    }
    return err.body || err.message;
  }
  return err instanceof Error ? err.message : String(err);
}

async function json<T>(res: Response): Promise<T> {
  return (await res.json()) as T;
}

// ── Adventures (= campaign modules) ─────────────────────────────────────────────

export async function listAdventures(): Promise<AdventureRead[]> {
  return json(await apiFetch("/api/adventures"));
}

export async function uploadAdventure(file: File, title?: string): Promise<AdventureRead> {
  const form = new FormData();
  form.append("file", file);
  if (title && title.trim()) form.append("title", title.trim());
  return json(await apiFetch("/api/adventures/upload", { method: "POST", body: form }));
}

/** Kick off background AI authoring of a campaign. Returns 202 with a processing stub. */
export async function generateAdventure(
  body: AdventureGenerateRequest,
): Promise<AdventureGenerateResponse> {
  return json(
    await apiFetch("/api/adventures/generate", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }),
  );
}

export async function deleteAdventure(slug: string): Promise<void> {
  await apiFetch(`/api/adventures/${encodeURIComponent(slug)}`, {
    method: "DELETE",
    headers: { "X-Confirm-Delete": "yes" },
  });
}

/** Get-or-create the playable campaign row for an adventure module. */
export async function getOrCreateCampaign(slug: string): Promise<CampaignRead> {
  return json(
    await apiFetch(`/api/adventures/${encodeURIComponent(slug)}/campaign`, { method: "POST" }),
  );
}

// ── Characters (PG) ─────────────────────────────────────────────────────────────

export async function listCharacters(): Promise<CharacterListItem[]> {
  return json(await apiFetch("/api/characters"));
}

export async function listCharacterPresets(): Promise<CharacterPreset[]> {
  return json(await apiFetch("/api/character-presets"));
}

export async function createCharacter(body: {
  name: string;
  character_class: string;
  level: number;
  hp_max: number;
  base_stats: Record<string, unknown>;
  base_inventory: Record<string, unknown>;
}): Promise<CharacterRead> {
  return json(
    await apiFetch("/api/characters", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }),
  );
}

export async function deleteCharacter(id: number): Promise<void> {
  await apiFetch(`/api/characters/${id}`, { method: "DELETE" });
}

// ── Sessions & campaign assignment ──────────────────────────────────────────────

export async function listSessions(campaignId: number): Promise<SessionRead[]> {
  return json(await apiFetch(`/api/sessions?campaign_id=${campaignId}`));
}

export async function createSession(
  campaignId: number,
  activeCharacterId?: number,
): Promise<SessionRead> {
  return json(
    await apiFetch("/api/sessions", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        campaign_id: campaignId,
        ...(activeCharacterId != null ? { active_character_id: activeCharacterId } : {}),
      }),
    }),
  );
}

export async function assignCharacterToCampaign(
  campaignId: number,
  characterId: number,
): Promise<void> {
  await apiFetch(`/api/campaigns/${campaignId}/characters`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ character_id: characterId }),
  });
}
