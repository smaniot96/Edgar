/**
 * Bearer token for hosted (token-mode) Edgar (EDG-160, builds on EDG-065).
 *
 * The token lives in `sessionStorage` only: it is scoped to this browser tab and cleared when the
 * tab closes. It is never written to `localStorage`, a URL, a log or an error message, and this
 * module never returns it for display. Local mode sends no token, so the header is omitted.
 *
 * `authorizedFetch` is the single place the header is added. Every request the client makes goes
 * through it, including JSON calls, SSE streams, uploads and binary resources. A static test
 * (`noDirectFetch.test.ts`) fails if any other module calls `fetch`, `EventSource` or XHR directly.
 */

const TOKEN_KEY = "edgar.apiToken";
// Tokens are `edgar_` plus URL-safe base64. Anything else is refused before it reaches a header.
const TOKEN_SHAPE = /^[\x21-\x7e]{1,512}$/;

/** `sessionStorage` if the browser exposes it, otherwise null (private modes can throw on access). */
function sessionStore(): Storage | null {
  try {
    return globalThis.sessionStorage ?? null;
  } catch {
    return null;
  }
}

/** The stored token, or null when none is set or the browser blocks session storage. */
export function getApiToken(): string | null {
  try {
    const value = sessionStore()?.getItem(TOKEN_KEY);
    return value && value.trim() ? value.trim() : null;
  } catch {
    return null;
  }
}

export function hasApiToken(): boolean {
  return getApiToken() !== null;
}

/** Store a token for this tab. Returns false when the value is malformed or storage is blocked. */
export function setApiToken(raw: string): boolean {
  const value = raw.trim();
  if (!TOKEN_SHAPE.test(value)) return false;
  try {
    const store = sessionStore();
    if (!store) return false;
    store.setItem(TOKEN_KEY, value);
    return true;
  } catch {
    return false;
  }
}

/** Forget the token for this tab. Always safe to call; a blocked store simply has nothing to clear. */
export function clearApiToken(): void {
  try {
    sessionStore()?.removeItem(TOKEN_KEY);
  } catch {
    /* nothing stored, or storage blocked: either way no token remains readable here */
  }
}

/** Init with `Authorization: Bearer <token>` added when a token is set; otherwise unchanged. */
export function withAuthorization(init?: RequestInit): RequestInit | undefined {
  const token = getApiToken();
  if (!token) return init;
  const headers = new Headers(init?.headers);
  headers.set("Authorization", `Bearer ${token}`);
  return { ...init, headers };
}

/** The one network entry point for the API client. Callers never call `fetch` themselves. */
export function authorizedFetch(url: string, init?: RequestInit): Promise<Response> {
  return fetch(url, withAuthorization(init));
}
