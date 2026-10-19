import { ApiError } from "../api/client";

/** The token in a `#token=…` fragment (or a bare fragment), else null. */
export function tokenFromHash(hash: string): string | null {
  const raw = hash.startsWith("#") ? hash.slice(1) : hash;
  if (!raw) return null;
  const value = raw.includes("=") ? new URLSearchParams(raw).get("token") : raw;
  return value && value.trim() ? value.trim() : null;
}

/** Copy for a failed accept. The server never says which invite condition failed, so neither do we. */
export function joinFailureCopy(err: unknown): { text: string; settings?: boolean } {
  if (err instanceof ApiError) {
    if (err.status === 401) {
      return { text: "Joining needs you to be signed in. Open Settings, enter your access token, then open the invite link again.", settings: true };
    }
    if (err.status === 404) {
      return { text: "This invite can't be used. It may have expired, been revoked or already been used, or it was meant for another account. Ask the owner for a new link." };
    }
    if (err.status === 422) {
      return { text: "A player seat needs a hero. Choose one of your characters that is already part of this campaign, then try again." };
    }
    if (err.status === 409) {
      return { text: "You already have a hero at this table, or that hero is seated. Pick a different hero, or open the table." };
    }
  }
  return { text: "" };
}


/** The shareable link. The token rides in the fragment, so it never reaches a server log or referrer. */
export function inviteLink(token: string): string {
  const base = import.meta.env.BASE_URL.endsWith("/") ? import.meta.env.BASE_URL : `${import.meta.env.BASE_URL}/`;
  return `${window.location.origin}${base}join#token=${encodeURIComponent(token)}`;
}
