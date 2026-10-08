/** "Resume last session" pointer, shared by the Campaigns page, the campaign hub and Play. */
export const LAST_SESSION_KEY = "lastSessionId";

export function readLastSessionId(): number | null {
  try {
    const raw = localStorage.getItem(LAST_SESSION_KEY);
    const n = raw == null ? NaN : Number(raw);
    return Number.isInteger(n) && n > 0 ? n : null;
  } catch {
    return null;
  }
}

export function rememberLastSession(id: number): void {
  try {
    localStorage.setItem(LAST_SESSION_KEY, String(id));
  } catch {
    /* best-effort */
  }
}

export function forgetLastSession(): void {
  try {
    localStorage.removeItem(LAST_SESSION_KEY);
  } catch {
    /* best-effort */
  }
}
