import { useCallback, useEffect, useRef, useState } from "react";

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
  const [settledRevision, setSettledRevision] = useState(0);
  const requestRevision = useRef(0);
  const [settledRequestRevision, setSettledRequestRevision] = useState(-1);
  const refresh = useCallback(() => {
    const next = ++requestRevision.current;
    setSnapshot((value) => value.sessionId === sessionId
      ? { ...value, loading: true }
      : { sessionId, data: null, loading: true, hasLoaded: false, error: null });
    setRevision(next);
    return next;
  }, [sessionId]);

  useEffect(() => {
    if (!enabled) return;
    const controller = new AbortController();
    const currentRequest = () => !controller.signal.aborted && requestRevision.current === revision;
    getSessionMap(sessionId, controller.signal)
      .then((result) => {
        if (currentRequest()) {
          setSnapshot((value) => !currentRequest()
            ? value
            : { ...value, sessionId, data: result, error: null });
        }
      })
      .catch((reason: unknown) => {
        if (currentRequest()) {
          setSnapshot((value) => !currentRequest()
            ? value
            : { ...value, sessionId, error: reason });
        }
      })
      .finally(() => {
        if (currentRequest()) {
          setSettledRevision((value) => value + 1);
          setSettledRequestRevision(revision);
          setSnapshot((value) => !currentRequest()
            ? value
            : { ...value, sessionId, loading: false, hasLoaded: true });
        }
      });
    return () => controller.abort();
  }, [enabled, revision, sessionId]);

  return {
    settledRevision,
    settledRequestRevision,
    data: enabled ? current.data : null,
    loading: enabled && (!current.hasLoaded || current.loading),
    error: enabled ? current.error : null,
    refresh,
  };
}
