/**
 * Images and downloads from the API (EDG-160).
 *
 * Without a token (local mode) the plain URL is used exactly as before: `<img src>` and `<a href>`
 * let the browser load and cache it. With a token the browser cannot send the Authorization header
 * on those navigations, so the client fetches the bytes and shows or saves them from an object URL.
 */
import { useEffect, useState } from "react";

import { hasApiToken } from "./auth";
import { fetchBlob } from "./client";

interface ObjectUrlState {
  /** The URL this state was produced for, so a stale object URL is never shown for a new one. */
  forUrl: string | null;
  objectUrl: string | null;
  error: unknown;
}

const EMPTY: ObjectUrlState = { forUrl: null, objectUrl: null, error: null };

/**
 * The URL to show an API resource at. `url` is the full URL (including any API base); pass null for
 * nothing. Without a token the plain URL is returned and nothing is fetched. With a token the bytes
 * are fetched with the header and returned as an object URL, which is revoked when the URL changes
 * or the component unmounts.
 */
export function useAuthorizedObjectUrl(url: string | null): { objectUrl: string | null; error: unknown } {
  const [state, setState] = useState<ObjectUrlState>(EMPTY);
  const authorized = hasApiToken();

  useEffect(() => {
    if (url === null || !authorized) return;
    const ctrl = new AbortController();
    let created: string | null = null;
    fetchBlob(url, ctrl.signal)
      .then((blob) => {
        if (ctrl.signal.aborted) return;
        created = URL.createObjectURL(blob);
        setState({ forUrl: url, objectUrl: created, error: null });
      })
      .catch((error: unknown) => {
        if (ctrl.signal.aborted) return;
        setState({ forUrl: url, objectUrl: null, error });
      });
    return () => {
      ctrl.abort();
      if (created) URL.revokeObjectURL(created);
    };
  }, [url, authorized]);

  if (url === null) return { objectUrl: null, error: null };
  if (!authorized) return { objectUrl: url, error: null };
  if (state.forUrl !== url) return { objectUrl: null, error: null };
  return { objectUrl: state.objectUrl, error: state.error };
}

/** Save an API resource under `filename`: the authorized client when a token is set, else a plain navigation. */
export async function downloadAuthorized(url: string, filename: string): Promise<void> {
  const blob = await fetchBlob(url);
  const objectUrl = URL.createObjectURL(blob);
  try {
    const link = document.createElement("a");
    link.href = objectUrl;
    link.download = filename;
    document.body.append(link);
    link.click();
    link.remove();
  } finally {
    // Give the browser a moment to start the download before the object URL goes away.
    window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
  }
}
