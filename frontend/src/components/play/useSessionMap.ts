import { useCallback, useEffect, useState } from "react";

import { getSessionMap } from "../../api/client";
import type { SessionMapRead } from "../../api/types";

export function useSessionMap(sessionId: number, enabled: boolean) {
  const [snapshot, setSnapshot] = useState<{
    sessionId: number;
    data: SessionMapRead | null;
    loading: boolean;
    hasLoaded: boolean;
    error: unknown;
  }>(() => ({ sessionId, data: null, loading: false, hasLoaded: false, error: null }));
  const current = snapshot.sessionId === sessionId
    ? snapshot
    : { sessionId, data: null, loading: false, hasLoaded: false, error: null };
  const [revision, setRevision] = useState(0);
  const refresh = useCallback(() => {
    setSnapshot((value) => value.sessionId === sessionId
      ? { ...value, loading: true }
      : { sessionId, data: null, loading: true, hasLoaded: false, error: null });
    setRevision((value) => value + 1);
  }, [sessionId]);

  useEffect(() => {
    if (!enabled) return;
    const controller = new AbortController();
    getSessionMap(sessionId, controller.signal)
      .then((result) => {
        if (!controller.signal.aborted) {
          setSnapshot((value) => controller.signal.aborted
            ? value
            : { ...value, sessionId, data: result, error: null });
        }
      })
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) {
          setSnapshot((value) => controller.signal.aborted
            ? value
            : { ...value, sessionId, error: reason });
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) {
          setSnapshot((value) => controller.signal.aborted
            ? value
            : { ...value, sessionId, loading: false, hasLoaded: true });
        }
      });
    return () => controller.abort();
  }, [enabled, revision, sessionId]);

  return {
    data: enabled ? current.data : null,
    loading: enabled && (!current.hasLoaded || current.loading),
    error: enabled ? current.error : null,
    refresh,
  };
}
