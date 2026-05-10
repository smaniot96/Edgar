import { useCallback } from "react";

import { readSse } from "../api/sse";

export interface TurnStreamHandlers {
  onStatus: (stage: string) => void;
  onToken: (text: string) => void;
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
        /* structured result; play UI ignores until a dedicated panel exists */
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
