/**
 * Images and downloads from the API, fetched with the bearer token (EDG-160).
 *
 * A plain `<img src>` or `<a href>` makes a navigation the browser sends without an Authorization
 * header, which token mode refuses. These helpers fetch the bytes through the authorized client and
 * show or save them from an object URL instead.
 */
import { useEffect, useState } from "react";

import { fetchBlob } from "./client";

interface ObjectUrlState {
  /** The URL this state was produced for, so a stale object URL is never shown for a new one. */
  forUrl: string | null;
  objectUrl: string | null;
  error: unknown;
}

const EMPTY: ObjectUrlState = { forUrl: null, objectUrl: null, error: null };

/**
 * An object URL for an authorized API resource. `url` is the full URL (including any API base);
 * pass null to show nothing. The object URL is revoked when the URL changes or the component unmounts.
 */
export function useAuthorizedObjectUrl(url: string | null): { objectUrl: string | null; error: unknown } {
  const [state, setState] = useState<ObjectUrlState>(EMPTY);

  useEffect(() => {
    if (url === null) return;
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
  }, [url]);

  if (url === null || state.forUrl !== url) return { objectUrl: null, error: null };
  return { objectUrl: state.objectUrl, error: state.error };
}

/** Save an authorized API resource to the user's downloads under `filename`. */
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
