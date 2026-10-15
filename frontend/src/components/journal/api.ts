import { useCallback, useEffect, useRef, useState } from "react";

import { apiFetch } from "../../api/client";
import type { JournalRead } from "./types";

export type { JournalCategory, JournalEntry, JournalRead } from "./types";

interface JournalState {
  sessionId: number | null;
  data: JournalRead | null;
  loading: boolean;
  error: unknown;
  stale: boolean;
}

const EMPTY: JournalState = {
  sessionId: null,
  data: null,
  loading: false,
  error: null,
  stale: false,
};

/** Loads the selected session journal and retains its last good snapshot during refresh failures. */
export function useJournal(sessionId: number | null) {
  const [state, setState] = useState<JournalState>(EMPTY);
  const [attempt, setAttempt] = useState(0);
  const activeSessionId = useRef<number | null>(null);
  const lastGood = useRef<JournalRead | null>(null);

  useEffect(() => {
    if (sessionId === null || !Number.isSafeInteger(sessionId) || sessionId < 1) {
      activeSessionId.current = null;
      lastGood.current = null;
      return;
    }

    if (activeSessionId.current !== sessionId) {
      activeSessionId.current = sessionId;
      lastGood.current = null;
    }
    const retainedData = lastGood.current;
    const controller = new AbortController();
    setState({
      sessionId,
      data: retainedData,
      loading: true,
      error: null,
      stale: retainedData !== null,
    });

    void apiFetch(`/api/sessions/${sessionId}/journal`, { signal: controller.signal })
      .then((response) => response.json() as Promise<JournalRead>)
      .then(
        (data) => {
          if (controller.signal.aborted || activeSessionId.current !== sessionId) return;
          lastGood.current = data;
          setState({ sessionId, data, loading: false, error: null, stale: false });
        },
        (error: unknown) => {
          if (controller.signal.aborted || activeSessionId.current !== sessionId) return;
          setState({
            sessionId,
            data: retainedData,
            loading: false,
            error,
            stale: retainedData !== null,
          });
        },
      );
    return () => controller.abort();
  }, [sessionId, attempt]);

  const refresh = useCallback(() => setAttempt((value) => value + 1), []);
  const currentSession = sessionId !== null && state.sessionId === sessionId;
  return {
    data: currentSession ? state.data : null,
    loading: sessionId !== null && (!currentSession || state.loading),
    error: currentSession ? state.error : null,
    stale: currentSession && state.stale,
    refresh,
  };
}
