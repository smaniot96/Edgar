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
  settledRevision: number;
}

const EMPTY: JournalState = {
  sessionId: null,
  data: null,
  loading: false,
  error: null,
  stale: false,
  settledRevision: 0,
};

/** Loads the selected session journal and retains its last good snapshot during refresh failures. */
export function useJournal(sessionId: number | null) {
  const [state, setState] = useState<JournalState>(EMPTY);
  const [attempt, setAttempt] = useState(0);
  const activeSessionId = useRef<number | null>(null);
  const lastGood = useRef<JournalRead | null>(null);
  const settledRevision = useRef(0);

  useEffect(() => {
    if (sessionId === null || !Number.isSafeInteger(sessionId) || sessionId < 1) {
      activeSessionId.current = null;
      lastGood.current = null;
      settledRevision.current = 0;
      return;
    }

    if (activeSessionId.current !== sessionId) {
      activeSessionId.current = sessionId;
      lastGood.current = null;
      settledRevision.current = 0;
    }
    const retainedData = lastGood.current;
    const controller = new AbortController();
    setState({
      sessionId,
      data: retainedData,
      loading: true,
      error: null,
      stale: retainedData !== null,
      settledRevision: settledRevision.current,
    });

    void apiFetch(`/api/sessions/${sessionId}/journal`, { signal: controller.signal })
      .then((response) => response.json() as Promise<JournalRead>)
      .then(
        (data) => {
          if (controller.signal.aborted || activeSessionId.current !== sessionId) return;
          lastGood.current = data;
          settledRevision.current += 1;
          setState({ sessionId, data, loading: false, error: null, stale: false, settledRevision: settledRevision.current });
        },
        (error: unknown) => {
          if (controller.signal.aborted || activeSessionId.current !== sessionId) return;
          settledRevision.current += 1;
          setState({
            sessionId,
            data: retainedData,
            loading: false,
            error,
            stale: retainedData !== null,
            settledRevision: settledRevision.current,
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
    settledRevision: currentSession ? state.settledRevision : 0,
    refresh,
  };
}
