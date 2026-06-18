import { useCallback } from "react";

import { readSse } from "../api/sse";
import type { AdjudicationResult, DebugPayload } from "../api/types";

export interface TurnStreamHandlers {
  onStatus: (stage: string) => void;
  onToken: (text: string) => void;
  onAdjudication: (result: AdjudicationResult) => void;
  /** Fires once per turn (only when debug=true was requested), just before done. */
  onDebug?: (payload: DebugPayload) => void;
  onDone: (data: Record<string, unknown>) => void;
  onError: (detail: string) => void;
}

/** Bridges `readSse` into handlers for the play UI turn stream. */
export function useSseTurnStream() {
  const consume = useCallback(async (response: Response, handlers: TurnStreamHandlers) => {
    for await (const { event, data } of readSse(response)) {
      if (event === "status") {
        const stage = typeof data.stage === "string" ? data.stage : "";
        handlers.onStatus(stage);
      } else if (event === "token") {
        const text = typeof data.text === "string" ? data.text : "";
        handlers.onToken(text);
      } else if (event === "adjudication") {
        handlers.onAdjudication(data as unknown as AdjudicationResult);
      } else if (event === "debug") {
        handlers.onDebug?.(data as unknown as DebugPayload);
      } else if (event === "done") {
        handlers.onDone(data);
        return;
      } else if (event === "error") {
        const detail =
          typeof data.detail === "string" ? data.detail : JSON.stringify(data);
        handlers.onError(detail);
        return;
      }
    }
  }, []);

  return { consume };
}
