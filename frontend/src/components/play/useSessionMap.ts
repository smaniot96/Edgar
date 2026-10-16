import { useCallback, useEffect, useState } from "react";

import { getSessionMap } from "../../api/client";
import type { SessionMapRead } from "../../api/types";

export function useSessionMap(sessionId: number, enabled: boolean) {
  const [data, setData] = useState<SessionMapRead | null>(null);
  const [loading, setLoading] = useState(false);
  const [hasLoaded, setHasLoaded] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [revision, setRevision] = useState(0);
  const refresh = useCallback(() => {
    setLoading(true);
    setRevision((value) => value + 1);
  }, []);

  useEffect(() => {
    if (!enabled) return;
    const controller = new AbortController();
    getSessionMap(sessionId, controller.signal)
      .then((result) => {
        setData(result);
        setError(null);
      })
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) setError(reason);
      })
      .finally(() => {
        if (!controller.signal.aborted) {
          setLoading(false);
          setHasLoaded(true);
        }
      });
    return () => controller.abort();
  }, [enabled, revision, sessionId]);

  return { data, loading: enabled && (!hasLoaded || loading), error, refresh };
}
