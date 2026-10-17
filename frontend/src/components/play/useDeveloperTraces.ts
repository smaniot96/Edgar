import { useEffect } from "react";

import { isNotFound, listSessionTurnTraces } from "../../api/client";
import type { ChatMessageRead } from "../../api/types";
import type { TurnAction } from "./turnState";

/**
 * Developer mode only (EDG-138): after the history is seeded, load the stored developer traces and
 * attach them to the reloaded DM turns. Player mode never requests them. Re-runs when the boot
 * snapshot changes, so a reset of the history is always followed by a fresh lookup.
 */
export function useDeveloperTraces(
  sessionId: number,
  enabled: boolean,
  boot: { history: ChatMessageRead[] } | null,
  dispatch: (action: TurnAction) => void,
) {
  useEffect(() => {
    if (!enabled || !boot) return;
    const ctrl = new AbortController();
    dispatch({ type: "traces", lookup: { status: "loading" } });
    listSessionTurnTraces(sessionId, ctrl.signal)
      .then((list) => {
        if (ctrl.signal.aborted) return;
        dispatch({
          type: "traces",
          lookup: { status: "loaded", records: list.turns, truncated: list.truncated },
        });
      })
      .catch((error: unknown) => {
        if (ctrl.signal.aborted) return;
        dispatch({
          type: "traces",
          lookup: { status: isNotFound(error) ? "disabled" : "failed" },
        });
      });
    return () => ctrl.abort();
    // The boot snapshot identifies one history load; dispatch is stable.
  }, [sessionId, enabled, boot, dispatch]);
}
