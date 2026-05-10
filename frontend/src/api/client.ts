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
