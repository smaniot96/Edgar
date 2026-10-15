import { useCallback, useEffect, useState } from "react";

export interface SessionDraft {
  text: string;
  revision: number;
  pendingRequest: { requestId: string; input: string; draftRevision?: number } | null;
}

const EMPTY: SessionDraft = { text: "", revision: 0, pendingRequest: null };

function storageKey(sessionId: number): string {
  return `edgar_play_draft:v1:${sessionId}`;
}

function readDraft(sessionId: number): SessionDraft {
  try {
    const value = localStorage.getItem(storageKey(sessionId));
    if (!value) return EMPTY;
    const parsed = JSON.parse(value) as Partial<SessionDraft>;
    if (typeof parsed.text === "string" && Number.isSafeInteger(parsed.revision) && (parsed.revision ?? -1) >= 0) {
      const pending = parsed.pendingRequest;
      const pendingRequest = pending && typeof pending === "object" &&
        typeof pending.requestId === "string" && typeof pending.input === "string"
        ? { requestId: pending.requestId, input: pending.input, ...(Number.isSafeInteger(pending.draftRevision) ? { draftRevision: pending.draftRevision as number } : {}) }
        : null;
      return { text: parsed.text, revision: parsed.revision as number, pendingRequest };
    }
  } catch {
    /* Storage may be disabled or contain an older, malformed value. */
  }
  return EMPTY;
}

/** Persists an unsent action by session, and clears it only for the revision a server acknowledged. */
export function useSessionDraft(sessionId: number) {
  const [draft, setDraft] = useState<SessionDraft>(() => readDraft(sessionId));

  useEffect(() => {
    try {
      if (draft.text || draft.pendingRequest) localStorage.setItem(storageKey(sessionId), JSON.stringify(draft));
      else localStorage.removeItem(storageKey(sessionId));
    } catch {
      /* Draft persistence is best-effort when browser storage is unavailable. */
    }
  }, [sessionId, draft]);

  const edit = useCallback((text: string) => {
    setDraft((current) => ({ ...current, text, revision: current.revision + 1 }));
  }, []);

  const clearIfRevision = useCallback((revision: number | undefined) => {
    if (revision == null) return;
    setDraft((current) => current.revision === revision
      ? { ...current, text: "", revision: current.revision + 1 }
      : current);
  }, []);

  const beginRequest = useCallback((request: NonNullable<SessionDraft["pendingRequest"]>) => {
    setDraft((current) => {
      const next = { ...current, pendingRequest: request };
      try { localStorage.setItem(storageKey(sessionId), JSON.stringify(next)); } catch { /* best effort */ }
      return next;
    });
  }, [sessionId]);

  const clearRequest = useCallback((requestId: string) => {
    setDraft((current) => {
      if (current.pendingRequest?.requestId !== requestId) return current;
      const next = { ...current, pendingRequest: null };
      try {
        if (next.text) localStorage.setItem(storageKey(sessionId), JSON.stringify(next));
        else localStorage.removeItem(storageKey(sessionId));
      } catch { /* best effort */ }
      return next;
    });
  }, [sessionId]);

  return { draft: draft.text, revision: draft.revision, edit, clearIfRevision, pendingRequest: draft.pendingRequest, beginRequest, clearRequest };
}
