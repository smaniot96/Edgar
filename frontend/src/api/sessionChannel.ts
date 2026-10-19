/**
 * Client for the session broadcast channel (EDG-047, docs/session-broadcast.md).
 *
 * Subscribes with `fetch` streaming through `authorizedFetch`, so the bearer token travels only in
 * the Authorization header. Keeps the last applied sequence number, drops any record at or below
 * it (at-least-once delivery from the server is therefore harmless), and resumes after a dropped
 * connection with `Last-Event-ID`. The server sends `snapshot` when the client is too far behind
 * or new; the handler replaces its shared state from it.
 *
 * No React here: the lobby and roster UI (EDG-050) subscribes through this module.
 */

import { authorizedFetch } from "./auth";
import { readSse } from "./sse";

export type ChannelChannel = "table" | "narration" | "rolls" | "system" | "private";

export interface ChannelActor {
  user_id: number | null;
  display_name: string | null;
  character_id: number | null;
  character_name: string | null;
}

/** One ordered event from the channel. `seq` is the session sequence number. */
export interface ChannelEnvelope {
  seq: number;
  type: string;
  channel: ChannelChannel;
  actor: ChannelActor | null;
  turn_id: string | null;
  at: string;
  data: Record<string, unknown>;
}

export type ChannelStatus = "connecting" | "live" | "reconnecting" | "closed";

export interface SessionChannelHandlers {
  /** Every new record, once, in sequence order. */
  onEvent: (envelope: ChannelEnvelope) => void;
  /** Replace shared state from a resync snapshot (the client was behind the window or new). */
  onSnapshot?: (envelope: ChannelEnvelope) => void;
  onStatus?: (status: ChannelStatus, detail?: string) => void;
}

export interface SessionChannelOptions {
  sessionId: number;
  /** Prefix for API paths; empty for the bundled UI, which is served from the API's origin. */
  apiBase?: string;
  /** The last sequence already applied (for example, restored after a reload). */
  initialSeq?: number;
  retryMinMs?: number;
  retryMaxMs?: number;
}

export interface SessionChannel {
  stop: () => void;
  /** Resolves when the subscription has ended for good (stopped, or access was refused). */
  done: Promise<void>;
  lastSeq: () => number;
}

/** Whether a record with `seq` is new after `lastSeq`. The one rule for ordering and dedupe. */
export function isNewRecord(lastSeq: number, seq: number): boolean {
  return Number.isInteger(seq) && seq > lastSeq;
}

/** Closes that mean the subscription must not be retried: the membership is gone or never existed. */
const TERMINAL_STATUSES = new Set([401, 403, 404]);

function sleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    if (signal.aborted) return resolve();
    const timer = setTimeout(resolve, ms);
    signal.addEventListener(
      "abort",
      () => {
        clearTimeout(timer);
        resolve();
      },
      { once: true },
    );
  });
}

export function createSessionChannel(
  options: SessionChannelOptions,
  handlers: SessionChannelHandlers,
): SessionChannel {
  const base = options.apiBase ?? "";
  const retryMin = options.retryMinMs ?? 1000;
  const retryMax = options.retryMaxMs ?? 30000;
  const controller = new AbortController();
  let last = options.initialSeq ?? 0;
  let stopped = false;

  const finish = (status: ChannelStatus, detail?: string) => {
    stopped = true;
    handlers.onStatus?.(status, detail);
  };

  async function subscribeOnce(): Promise<"retry" | "terminal"> {
    const headers: Record<string, string> = { accept: "text/event-stream" };
    if (last > 0) headers["last-event-id"] = String(last);
    const res = await authorizedFetch(`${base}/api/sessions/${options.sessionId}/events`, {
      headers,
      signal: controller.signal,
    });
    if (TERMINAL_STATUSES.has(res.status)) return "terminal";
    if (!res.ok) throw new Error(`channel HTTP ${res.status}`);
    handlers.onStatus?.("live");
    for await (const frame of readSse(res)) {
      if (frame.event === "closed") {
        const reason = String(frame.data.reason ?? "");
        if (reason === "access_revoked") return "terminal";
        return "retry";
      }
      const envelope = frame.data as unknown as ChannelEnvelope;
      if (frame.event === "snapshot") {
        // The server sends a snapshot only when the cursor is unusable, and its head is the new cursor.
        last = envelope.seq;
        handlers.onSnapshot?.(envelope);
        continue;
      }
      if (!isNewRecord(last, envelope.seq)) continue;
      last = envelope.seq;
      handlers.onEvent(envelope);
    }
    return "retry";
  }

  const done = (async () => {
    let delay = retryMin;
    let reconnecting = false;
    while (!stopped) {
      if (reconnecting) handlers.onStatus?.("reconnecting");
      try {
        const outcome = await subscribeOnce();
        if (stopped) return;
        if (outcome === "terminal") return finish("closed", "access");
        // The server ended a live stream on purpose (shutdown, lifetime, slow consumer): reconnect soon.
        delay = retryMin;
      } catch (err) {
        if (controller.signal.aborted) return;
        handlers.onStatus?.("reconnecting", err instanceof Error ? err.message : "error");
      }
      if (stopped) return;
      reconnecting = true;
      await sleep(delay, controller.signal);
      delay = Math.min(delay * 2, retryMax);
    }
  })();

  return {
    stop: () => {
      if (stopped) return;
      stopped = true;
      controller.abort();
      handlers.onStatus?.("closed", "stopped");
    },
    done: done.then(() => undefined),
    lastSeq: () => last,
  };
}
