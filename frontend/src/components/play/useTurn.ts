import { useCallback, useEffect, useReducer, useRef, useState } from "react";

import { apiBase } from "../../api/client";
import type { TurnResult } from "../../api/types";
import { consumeTurnStream, parseTurnError, type TurnError } from "../../api/turnStream";
import { initialTurnState, turnReducer } from "./turnState";

/** Abort the turn if the server sends nothing (not even a keep-alive) for this long. */
export const TURN_WATCHDOG_MS = 90_000;

type AbortReason = "user" | "timeout" | "unmount";

interface ActiveTurn {
  ctrl: AbortController;
  reason: AbortReason | null;
  requestId: string;
}

export interface TurnRequestIdentity {
  requestId: string;
  input: string;
  draftRevision?: number;
}

export interface UseTurnOptions {
  /** Request the per-turn debug payload (dev mode). */
  debug: boolean;
  /** Turn committed successfully: apply character/combat/scene updates. */
  onResult?: (result: TurnResult) => void;
  /** Called synchronously before POST so the request identity survives a closed tab. */
  onRequestStarted?: (request: TurnRequestIdentity) => void;
  /** Called only after an authoritative committed result. */
  onAcknowledged?: (draftRevision: number | undefined, requestId: string) => void;
  /** Server says the campaign is over (409). */
  onCampaignEnded?: () => void;
  onSourcesUnavailable?: () => void;
  /** Refresh readiness after a deterministic preflight rejection. */
  onReadinessRejected?: () => void;
  /** Turn failed or was stopped: the caller should retain `input` in the composer. */
  onFailed?: (input: string, draftRevision?: number) => void;
  /** Turn finished in any way (success or failure), e.g. to restore focus. */
  onSettled?: () => void;
}

let turnSeq = 0;
function newTurnId(): string {
  turnSeq += 1;
  return `t${Date.now().toString(36)}-${turnSeq}`;
}

function newRequestId(): string {
  try {
    if (typeof crypto !== "undefined") return crypto.randomUUID();
  } catch { /* fall back to a process-unique value in older browsers */ }
  turnSeq += 1;
  return `r${Date.now().toString(36)}-${turnSeq}-${Math.random().toString(36).slice(2)}`;
}

function isCampaignEnded(code: string | undefined, message: string): boolean {
  void message;
  return code === "campaign_ended" || code === "completed";
}

const READINESS_REJECTION_CODES = new Set([
  "campaign_sources_unavailable", "source_unavailable", "campaign_source_unavailable",
  "source_missing", "preparation_source_invalid",
  "required_books_unavailable", "required_book_unavailable", "required_books_missing",
  "required_book_phb_unindexed", "required_book_dmg_unindexed", "required_book_mm_unindexed",
  "book_version_unavailable", "required_assets_unavailable",
  "adventure_manifest_review_required", "manifest_review_required", "review_required",
  "source_review_required", "source_review_needed", "adventure_review_needed",
  "saved_version_conflict",
  "character_not_assigned", "assignment_required", "character_assignment_required",
  "assignment_conflict", "choose_character", "character_assignment_conflict",
  "rules_edition_mismatch", "unsupported_edition", "unsupported_party",
  "unsupported_progression", "unsupported_hero_capability", "creature_identity_unresolved",
  "preparation_setup_required", "preparation_setup_invalid", "preparation_profile_invalid",
  "validation_failed", "invalid_request", "stage_validation_failed",
  "preparation_in_progress", "preparation_busy", "preparation_failed", "readiness_unavailable",
  "campaign_ended", "completed",
]);

function isReadinessRejection(code: string | undefined): boolean {
  return code !== undefined && READINESS_REJECTION_CODES.has(code);
}

function describeTurnFailure(error: TurnError): string {
  const next = error.recoveryAction === "repair_source" ? " Repair or choose an available source."
    : error.recoveryAction === "review_source" ? " Review the current adventure material before continuing."
      : error.recoveryAction === "assign_character" ? " Choose a character assigned to this campaign."
        : error.recoveryAction === "fix_input" ? " Check the information and correct it before continuing."
          : error.recoveryAction === "wait" ? " Check its status before trying again."
            : error.recoveryAction === "configure_provider" ? " Review the provider setup before continuing."
              : error.recoveryAction === "restart_preparation" ? " Review the setup and restart preparation."
                : error.recoveryAction === "read_history" ? " Your saved history remains available."
                  : error.recoveryAction === "retry" ? " The server confirms this action was not saved; you can retry it."
                    : error.recoveryAction === "contact_support" ? " Share the reference with support if the problem continues."
                      : " Check the saved result before trying again.";
  const reference = error.supportRef ? ` Reference: ${error.supportRef}.` : "";
  const guidance = error.message.includes(next.trim()) ? "" : next;
  return `${error.message}${guidance}${reference}`;
}

function receiptUrl(sessionId: number, requestId: string): string {
  return `${apiBase}/api/sessions/${sessionId}/turns/${encodeURIComponent(requestId)}`;
}

async function readReceipt(response: Response): Promise<TurnResult> {
  const body = await response.json() as { status?: string; result?: TurnResult };
  if (body.status !== "committed" || !body.result || typeof body.result !== "object") {
    throw new Error("The save status response was incomplete.");
  }
  return body.result;
}

/**
 * Runs player turns against the streaming endpoint. Client-generated request IDs make retries
 * safe: after a disconnect, the UI checks the authoritative receipt before offering a retry.
 */
export function useTurn(sessionId: number, options: UseTurnOptions) {
  const [state, dispatch] = useReducer(turnReducer, initialTurnState);
  const [checking, setChecking] = useState(false);
  const linesRef = useRef(state.lines);
  useEffect(() => { linesRef.current = state.lines; }, [state.lines]);
  const optsRef = useRef(options);
  useEffect(() => { optsRef.current = options; });
  const activeRef = useRef<ActiveTurn | null>(null);
  const checkRef = useRef(false);
  const checkCtrlRef = useRef<AbortController | null>(null);

  useEffect(() => {
    return () => {
      const active = activeRef.current;
      if (active) {
        active.reason = "unmount";
        active.ctrl.abort();
      }
      checkCtrlRef.current?.abort();
    };
  }, [sessionId]);

  const notifyCommitted = useCallback((result: TurnResult, requestId: string, draftRevision?: number) => {
    optsRef.current.onResult?.(result);
    optsRef.current.onAcknowledged?.(draftRevision, requestId);
  }, []);

  const checkReceipt = useCallback(async (
    turnId: string,
    requestId: string,
    draftRevision?: number,
  ) => {
    if (activeRef.current || checkRef.current) return;
    checkRef.current = true;
    setChecking(true);
    const ctrl = new AbortController();
    checkCtrlRef.current = ctrl;
    try {
      const response = await fetch(receiptUrl(sessionId, requestId), { headers: { accept: "application/json" }, signal: ctrl.signal });
      if (ctrl.signal.aborted) return;
      if (response.status === 200) {
        const result = await readReceipt(response);
        if (ctrl.signal.aborted) return;
        const alreadyLoaded = result.event_id != null && linesRef.current.some(
          (line) => line.role === "dm" && line.eventId === result.event_id,
        );
        dispatch({ type: "receiptCommitted", turnId, result });
        // Boot already holds current state when this historical receipt is in its log.
        // Acknowledge the draft without replaying old character snapshots or success chips.
        if (alreadyLoaded) optsRef.current.onAcknowledged?.(draftRevision, requestId);
        else notifyCommitted(result, requestId, draftRevision);
      } else if (response.status === 404) {
        const failure = parseTurnError(await response.text());
        if (ctrl.signal.aborted) return;
        if (failure.code === "turn_not_found" && failure.outcome === "not_committed") {
          dispatch({ type: "markRetryable", turnId, message: "The server confirms this action was not saved. You can retry it safely." });
        } else {
          dispatch({ type: "receiptPending", turnId, message: "Couldn't confirm whether this action was saved. Check again before retrying." });
        }
      } else if (response.status === 409) {
        dispatch({ type: "receiptPending", turnId, message: "The DM is still finishing this action. Check again before retrying." });
      } else {
        dispatch({ type: "receiptPending", turnId, message: "Couldn't confirm whether this action was saved. Check again before retrying." });
      }
    } catch {
      if (ctrl.signal.aborted) return;
      dispatch({ type: "receiptPending", turnId, message: "Couldn't reach the save-status check. Check again before retrying." });
    } finally {
      if (checkCtrlRef.current === ctrl) {
        checkCtrlRef.current = null;
        checkRef.current = false;
        setChecking(false);
      }
    }
  }, [sessionId, notifyCommitted]);

  /** Reconcile an action persisted before a page closed or the route changed. */
  const recover = useCallback(async (request: TurnRequestIdentity) => {
    const turnId = `recovered-${request.requestId}`;
    dispatch({ type: "recover", turnId, requestId: request.requestId, input: request.input, draftRevision: request.draftRevision });
    await checkReceipt(turnId, request.requestId, request.draftRevision);
  }, [checkReceipt]);

  const send = useCallback(async (
    rawInput: string,
    draftRevision?: number,
    existingRequestId?: string,
  ) => {
    const input = rawInput.trim();
    if (!input || activeRef.current || checkRef.current || !Number.isFinite(sessionId)) return;
    const uncertain = state.lines.find((line) => line.role === "error" && line.uncertain);
    if (uncertain && (uncertain.role !== "error" || uncertain.retryRequestId !== existingRequestId)) return;

    const requestId = existingRequestId ?? newRequestId();
    const turnId = newTurnId();
    const active: ActiveTurn = { ctrl: new AbortController(), reason: null, requestId };
    activeRef.current = active;
    // Persist identity before any bytes are sent, so a reload can look up the receipt.
    optsRef.current.onRequestStarted?.({ requestId, input, draftRevision });
    dispatch({ type: "start", turnId, requestId, draftRevision, input });

    let timer: ReturnType<typeof setTimeout> | undefined;
    const kick = () => {
      clearTimeout(timer);
      timer = setTimeout(() => {
        active.reason = "timeout";
        active.ctrl.abort();
      }, TURN_WATCHDOG_MS);
    };
    kick();

    const fail = (message: string, code?: string, reason: "error" | "stopped" = "error", uncertain = false, retryable = false) => {
      dispatch({ type: "fail", turnId, reason, message, code, uncertain, retryable });
      optsRef.current.onFailed?.(input, draftRevision);
      if (code === "campaign_sources_unavailable") optsRef.current.onSourcesUnavailable?.();
      if (isReadinessRejection(code)) optsRef.current.onReadinessRejected?.();
    };

    try {
      const res = await fetch(`${apiBase}/api/sessions/${sessionId}/turn/stream`, {
        method: "POST",
        headers: { "content-type": "application/json", accept: "text/event-stream" },
        body: JSON.stringify({ message: input, debug: optsRef.current.debug, request_id: requestId }),
        signal: active.ctrl.signal,
      });
      if (active.ctrl.signal.aborted) throw new DOMException("Turn aborted", "AbortError");
      kick();

      if (!res.ok) {
        const body = await res.text().catch(() => "");
        const err = parseTurnError(body);
        if (res.status === 409 && isCampaignEnded(err.code, err.message)) {
          dispatch({ type: "fail", turnId, reason: "ended", message: err.message, code: err.code });
          optsRef.current.onCampaignEnded?.();
          optsRef.current.onReadinessRejected?.();
          return;
        }
        const deterministic = err.category === "source" || err.category === "review"
          || err.category === "validation" || err.category === "assignment";
        const uncertain = err.outcome
          ? err.outcome !== "not_committed"
          : !deterministic && (res.status === 409 || res.status >= 500);
        fail(describeTurnFailure(err), err.code ?? "turn_failed", "error", uncertain, err.retryable === true);
        return;
      }

      const outcome = await consumeTurnStream(res, {
        onActivity: kick,
        onStatus: (next) => {
          dispatch({ type: "status", turnId, stage: next });
        },
        onToken: (text) => dispatch({ type: "token", turnId, text }),
        onAdjudication: (result) => dispatch({ type: "adjudication", turnId, result }),
        onDebug: (payload) => dispatch({ type: "debug", turnId, payload }),
      });

      if (active.ctrl.signal.aborted) throw new DOMException("Turn aborted", "AbortError");
      if (outcome.kind === "done") {
        dispatch({ type: "done", turnId, result: outcome.result });
        notifyCommitted(outcome.result, requestId, draftRevision);
      } else if (outcome.kind === "error") {
        if (isCampaignEnded(outcome.error.code, outcome.error.message)) {
          dispatch({ type: "fail", turnId, reason: "ended", message: outcome.error.message, code: outcome.error.code });
          optsRef.current.onCampaignEnded?.();
          optsRef.current.onReadinessRejected?.();
        } else {
          const deterministic = outcome.error.category === "source" || outcome.error.category === "review"
            || outcome.error.category === "validation" || outcome.error.category === "assignment";
          const uncertain = outcome.error.outcome
            ? outcome.error.outcome !== "not_committed"
            : !deterministic;
          fail(describeTurnFailure(outcome.error), outcome.error.code ?? "turn_failed", "error", uncertain, outcome.error.retryable === true);
        }
      } else {
        fail(`The connection closed before the DM finished. Check save status before retrying.`, "incomplete", "error", true);
      }
    } catch {
      if (active.reason === "unmount") return;
      if (active.reason === "user") {
        fail("You stopped the DM. Check save status before retrying.", "stopped", "stopped", true);
      } else if (active.reason === "timeout") {
        fail(`The DM didn't respond for ${Math.round(TURN_WATCHDOG_MS / 1000)} seconds. Check save status before retrying.`, "timeout", "error", true);
      } else {
        fail("The connection stopped before the result was confirmed. Check save status before retrying.", "interrupted", "error", true);
      }
    } finally {
      clearTimeout(timer);
      if (activeRef.current === active) activeRef.current = null;
      if (active.reason !== "unmount") optsRef.current.onSettled?.();
    }
  }, [sessionId, notifyCommitted, state.lines]);

  const stop = useCallback(() => {
    const active = activeRef.current;
    if (!active) return;
    active.reason = "user";
    active.ctrl.abort();
  }, []);

  /** Retry only with the stable identity preserved on the failed action. */
  const retry = useCallback((turnId: string, input: string, requestId?: string, draftRevision?: number) => {
    if (activeRef.current || checkRef.current) return;
    const line = state.lines.find((item) => item.role === "error" && item.turnId === turnId);
    if (!requestId || line?.role !== "error" || line.uncertain || line.retryable !== true) return;
    dispatch({ type: "clearTurn", turnId });
    void send(input, draftRevision, requestId);
  }, [send, state.lines]);

  const check = useCallback((turnId: string, requestId: string, _input: string, draftRevision?: number) => {
    void checkReceipt(turnId, requestId, draftRevision);
  }, [checkReceipt]);

  return { state, dispatch, send, stop, retry, checkReceipt: check, recover, busy: state.pending !== null || checking };
}
