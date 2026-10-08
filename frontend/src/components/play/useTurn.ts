import { useCallback, useEffect, useReducer, useRef } from "react";

import { apiBase } from "../../api/client";
import type { TurnResult } from "../../api/types";
import { consumeTurnStream, parseTurnError } from "../../hooks/useSSE";
import { initialTurnState, turnReducer } from "./turnState";

/** Abort the turn if the server sends nothing (not even a keep-alive) for this long. */
export const TURN_WATCHDOG_MS = 90_000;

type AbortReason = "user" | "timeout" | "unmount";

interface ActiveTurn {
  ctrl: AbortController;
  reason: AbortReason | null;
}

export interface UseTurnOptions {
  /** Request the per-turn debug payload (dev mode). */
  debug: boolean;
  /** Turn committed successfully: apply character/combat/scene updates. */
  onResult?: (result: TurnResult) => void;
  /** Server says the campaign is over (409). */
  onCampaignEnded?: () => void;
  /** Turn failed or was stopped: the caller should restore `input` to the composer. */
  onFailed?: (input: string) => void;
  /** Turn finished in any way (success or failure), e.g. to restore focus. */
  onSettled?: () => void;
}

let turnSeq = 0;
function newTurnId(): string {
  turnSeq += 1;
  return `t${Date.now().toString(36)}-${turnSeq}`;
}

function isCampaignEnded(code: string | undefined, message: string): boolean {
  return code === "campaign_ended" || /campaign has ended|has ended/i.test(message);
}

/**
 * Runs player turns against `POST /api/sessions/{id}/turn/stream`.
 *
 * Every turn ends in exactly one of `done` / `fail`: a stream that closes without a terminal
 * event, an HTTP error, a network error, a watchdog timeout or a user Stop all surface as a
 * failure (partial narration kept and marked interrupted, Retry offered). The in-flight request
 * is aborted on unmount / session change so the server releases its turn lock.
 */
export function useTurn(sessionId: number, options: UseTurnOptions) {
  const [state, dispatch] = useReducer(turnReducer, initialTurnState);
  const optsRef = useRef(options);
  useEffect(() => {
    optsRef.current = options;
  });
  const activeRef = useRef<ActiveTurn | null>(null);

  useEffect(() => {
    return () => {
      const active = activeRef.current;
      if (active) {
        active.reason = "unmount";
        active.ctrl.abort();
      }
    };
  }, [sessionId]);

  const send = useCallback(
    async (rawInput: string) => {
      const input = rawInput.trim();
      if (!input || activeRef.current || !Number.isFinite(sessionId)) return;

      const turnId = newTurnId();
      const active: ActiveTurn = { ctrl: new AbortController(), reason: null };
      activeRef.current = active;
      dispatch({ type: "start", turnId, input });

      let timer: ReturnType<typeof setTimeout> | undefined;
      const kick = () => {
        clearTimeout(timer);
        timer = setTimeout(() => {
          active.reason = "timeout";
          active.ctrl.abort();
        }, TURN_WATCHDOG_MS);
      };
      kick();

      let stage = "parsing";
      // Once the server is saving, a dropped connection may still have committed the turn.
      const unsaved = () =>
        stage === "saving"
          ? "The turn may still have been saved — reload to check before retrying."
          : "Nothing was saved.";
      const fail = (message: string, code?: string, reason: "error" | "stopped" = "error") => {
        dispatch({ type: "fail", turnId, reason, message, code });
        optsRef.current.onFailed?.(input);
      };

      try {
        const res = await fetch(`${apiBase}/api/sessions/${sessionId}/turn/stream`, {
          method: "POST",
          headers: { "content-type": "application/json", accept: "text/event-stream" },
          body: JSON.stringify({ message: input, debug: optsRef.current.debug }),
          signal: active.ctrl.signal,
        });
        kick();

        if (!res.ok) {
          const body = await res.text().catch(() => "");
          const err = parseTurnError(body, `The server returned an error (${res.status}).`);
          if (res.status === 409 && isCampaignEnded(err.code, err.message)) {
            dispatch({ type: "fail", turnId, reason: "ended", message: err.message, code: err.code });
            optsRef.current.onCampaignEnded?.();
            return;
          }
          const message =
            res.status === 409
              ? "The DM is still resolving a previous action. Wait a moment, then retry."
              : err.message;
          fail(message, err.code ?? `http_${res.status}`);
          return;
        }

        const outcome = await consumeTurnStream(res, {
          onActivity: kick,
          onStatus: (next) => {
            stage = next;
            dispatch({ type: "status", turnId, stage: next });
          },
          onToken: (text) => dispatch({ type: "token", turnId, text }),
          onAdjudication: (result) => dispatch({ type: "adjudication", turnId, result }),
          onDebug: (payload) => dispatch({ type: "debug", turnId, payload }),
        });

        if (outcome.kind === "done") {
          dispatch({ type: "done", turnId, result: outcome.result });
          optsRef.current.onResult?.(outcome.result);
        } else if (outcome.kind === "error") {
          if (isCampaignEnded(outcome.error.code, outcome.error.message)) {
            dispatch({ type: "fail", turnId, reason: "ended", message: outcome.error.message });
            optsRef.current.onCampaignEnded?.();
          } else {
            fail(`The turn could not be completed: ${outcome.error.message}`, outcome.error.code);
          }
        } else {
          fail(`The connection closed before the DM finished. ${unsaved()}`, "incomplete");
        }
      } catch (e) {
        if (active.reason === "unmount") return;
        if (active.reason === "user") {
          fail(`You stopped the DM. ${unsaved()}`, "stopped", "stopped");
        } else if (active.reason === "timeout") {
          fail(
            `The DM didn't respond for ${Math.round(TURN_WATCHDOG_MS / 1000)} seconds. ${unsaved()}`,
            "timeout",
          );
        } else {
          const msg = e instanceof Error ? e.message : String(e);
          fail(`Connection problem (${msg}). ${unsaved()}`, "network");
        }
      } finally {
        clearTimeout(timer);
        if (activeRef.current === active) activeRef.current = null;
        if (active.reason !== "unmount") optsRef.current.onSettled?.();
      }
    },
    [sessionId],
  );

  const stop = useCallback(() => {
    const active = activeRef.current;
    if (!active) return;
    active.reason = "user";
    active.ctrl.abort();
  }, []);

  /** Drop the failed turn's lines and send its input again. */
  const retry = useCallback(
    (turnId: string, input: string) => {
      if (activeRef.current) return;
      dispatch({ type: "clearTurn", turnId });
      void send(input);
    },
    [send],
  );

  return { state, dispatch, send, stop, retry, busy: state.pending !== null };
}
