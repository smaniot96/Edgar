import { apiBase } from "../api/client";

const SAFE_SEGMENT = /^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$/;

function safeRelativePath(value: string): string | null {
  if (
    !value ||
    value.length > 1024 ||
    /[\\%?#:]/.test(value) ||
    [...value].some((character) => {
      const code = character.charCodeAt(0);
      return code < 32 || code === 127;
    })
  ) return null;
  const segments = value.split("/");
  if (segments.length > 32 || segments.some((part) => part === "." || part === ".." || !SAFE_SEGMENT.test(part))) {
    return null;
  }
  return value;
}

/**
 * Resolve the stored adventure cover contract to a same-origin/browser URL.
 * `assets/...` follows Vite's configured SPA base, legacy `ui/assets/...` stays under
 * `/ui/assets`, and `api/...` remains at the API root (including a configured API base).
 * Unsupported or unsafe metadata returns null so the card can show its initial fallback.
 */
export function resolveAdventureCoverUrl(
  coverImage: string | null | undefined,
  spaBaseUrl = import.meta.env.BASE_URL,
): string | null {
  if (typeof coverImage !== "string" || !coverImage || coverImage !== coverImage.trim()) return null;

  let relative: string;
  let kind: "asset" | "api";
  if (coverImage.startsWith("assets/")) {
    relative = coverImage.slice("assets/".length);
    kind = "asset";
  } else if (coverImage.startsWith("ui/assets/")) {
    relative = coverImage.slice("ui/assets/".length);
    kind = "asset";
  } else if (coverImage.startsWith("/ui/assets/")) {
    relative = coverImage.slice("/ui/assets/".length);
    kind = "asset";
  } else if (coverImage.startsWith("/api/")) {
    relative = coverImage.slice("/api/".length);
    kind = "api";
  } else if (coverImage.startsWith("api/")) {
    relative = coverImage.slice("api/".length);
    kind = "api";
  } else {
    return null;
  }

  const safe = safeRelativePath(relative);
  if (!safe) return null;
  if (kind === "api") {
    return `${apiBase.replace(/\/+$/, "")}/api/${safe}`;
  }
  const base = spaBaseUrl.endsWith("/") ? spaBaseUrl : `${spaBaseUrl}/`;
  return `${base}assets/${safe}`;
}
