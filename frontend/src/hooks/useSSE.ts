import { readSse } from "../api/sse";
import type { AdjudicationResult, DebugPayload, TurnResult } from "../api/types";

export interface TurnStreamHandlers {
  onStatus?: (stage: string) => void;
  onToken?: (text: string) => void;
  onAdjudication?: (result: AdjudicationResult) => void;
  /** Fires once per turn (only when debug=true was requested), just before done. */
  onDebug?: (payload: DebugPayload) => void;
  /** Any bytes received (events or keep-alive comments) — used to feed a watchdog. */
  onActivity?: () => void;
}

export interface TurnError {
  message: string;
  code?: string;
}

/** How a turn stream ended. `incomplete` = the connection closed with no terminal event. */
export type TurnStreamOutcome =
  | { kind: "done"; result: TurnResult }
  | { kind: "error"; error: TurnError }
  | { kind: "incomplete" };

function str(v: unknown): string | undefined {
  return typeof v === "string" && v.trim() ? v : undefined;
}

/**
 * Normalise an error payload into `{message, code}`. Accepts the SSE `error` frame
 * (`{detail}` or `{code, message}`), FastAPI bodies (`{detail: "..."}` or
 * `{detail: {code, message}}`) and raw text.
 */
export function parseTurnError(payload: unknown, fallback = "Something went wrong."): TurnError {
  if (typeof payload === "string") {
    const text = payload.trim();
    if (!text) return { message: fallback };
    try {
      return parseTurnError(JSON.parse(text) as unknown, fallback);
    } catch {
      return { message: text };
    }
  }
  if (!payload || typeof payload !== "object") return { message: fallback };
  const p = payload as Record<string, unknown>;
  let code = str(p.code);
  let message = str(p.message);
  const detail = p.detail;
  if (!message && typeof detail === "string") message = str(detail);
  if (detail && typeof detail === "object" && !Array.isArray(detail)) {
    const d = detail as Record<string, unknown>;
    message = message ?? str(d.message) ?? str(d.detail);
    code = code ?? str(d.code);
  }
  if (!message && Array.isArray(detail) && detail.length) {
    // FastAPI validation errors: [{msg, loc, ...}]
    const first = detail[0] as Record<string, unknown> | undefined;
    message = str(first?.msg);
  }
  return code ? { message: message ?? fallback, code } : { message: message ?? fallback };
}

/**
 * Read the turn SSE stream to its terminal event, dispatching intermediate events to handlers.
 * Never silently swallows a premature end: returns `{kind: "incomplete"}` instead.
 * Network/abort errors propagate as exceptions.
 */
export async function consumeTurnStream(
  response: Response,
  handlers: TurnStreamHandlers,
): Promise<TurnStreamOutcome> {
  for await (const { event, data } of readSse(response, { onActivity: handlers.onActivity })) {
    if (event === "status") {
      handlers.onStatus?.(typeof data.stage === "string" ? data.stage : "");
    } else if (event === "token") {
      if (typeof data.text === "string" && data.text) handlers.onToken?.(data.text);
    } else if (event === "adjudication") {
      handlers.onAdjudication?.(data as unknown as AdjudicationResult);
    } else if (event === "debug") {
      handlers.onDebug?.(data as unknown as DebugPayload);
    } else if (event === "done") {
      return { kind: "done", result: data as TurnResult };
    } else if (event === "error") {
      return { kind: "error", error: parseTurnError(data, "The turn could not be completed.") };
    }
  }
  return { kind: "incomplete" };
}
