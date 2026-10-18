import { afterEach, describe, expect, it, vi } from "vitest";

import {
  type ChannelEnvelope,
  type ChannelStatus,
  createSessionChannel,
  isNewRecord,
} from "./sessionChannel";

function envelopeFrame(seq: number, type = "narration.committed", data: Record<string, unknown> = {}): string {
  const envelope: ChannelEnvelope = {
    seq,
    type,
    channel: "table",
    actor: null,
    turn_id: null,
    at: "2026-10-18T00:00:00Z",
    data,
  };
  return `id: ${seq}\nevent: ${type}\ndata: ${JSON.stringify(envelope)}\n\n`;
}

function closedFrame(reason: string): string {
  return `event: closed\ndata: ${JSON.stringify({ reason })}\n\n`;
}

/** A response whose body delivers `frames` and then ends (or stays open when `hold` is set). */
function streamResponse(frames: string[], opts: { hold?: boolean; signal?: AbortSignal } = {}): Response {
  const enc = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const f of frames) controller.enqueue(enc.encode(f));
      if (!opts.hold) controller.close();
      opts.signal?.addEventListener("abort", () => controller.error(new DOMException("aborted", "AbortError")));
    },
  });
  return new Response(body, { status: 200, headers: { "content-type": "text/event-stream" } });
}

function stubFetch(responses: Array<(init: RequestInit) => Response>) {
  const calls: Array<{ url: string; headers: Headers }> = [];
  const fn = vi.fn(async (url: string, init: RequestInit = {}) => {
    calls.push({ url, headers: new Headers(init.headers) });
    const next = responses.shift();
    if (!next) throw new Error("unexpected extra request");
    return next(init);
  });
  vi.stubGlobal("fetch", fn);
  return { calls, fn };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("isNewRecord", () => {
  it("accepts only integers above the last applied sequence", () => {
    expect(isNewRecord(0, 1)).toBe(true);
    expect(isNewRecord(5, 6)).toBe(true);
    expect(isNewRecord(5, 5)).toBe(false);
    expect(isNewRecord(5, 4)).toBe(false);
    expect(isNewRecord(5, Number.NaN)).toBe(false);
    expect(isNewRecord(5, 5.5)).toBe(false);
  });
});

describe("createSessionChannel", () => {
  it("delivers each record once, in order, and drops duplicates", async () => {
    const events: number[] = [];
    const { calls } = stubFetch([
      () => streamResponse([envelopeFrame(1), envelopeFrame(2), envelopeFrame(2), envelopeFrame(3), closedFrame("server_shutdown")]),
      () => streamResponse([closedFrame("access_revoked")]),
    ]);
    const channel = createSessionChannel(
      { sessionId: 4, retryMinMs: 1, retryMaxMs: 2 },
      { onEvent: (e) => events.push(e.seq) },
    );
    await channel.done;
    expect(events).toEqual([1, 2, 3]);
    expect(calls[1].headers.get("last-event-id")).toBe("3");
  });

  it("resumes with Last-Event-ID after a dropped connection, and sends none on the first request", async () => {
    const { calls } = stubFetch([
      () => streamResponse([envelopeFrame(1), envelopeFrame(2)]),
      () => streamResponse([closedFrame("access_revoked")]),
    ]);
    const channel = createSessionChannel({ sessionId: 9, retryMinMs: 1, retryMaxMs: 2 }, { onEvent: () => {} });
    await channel.done;
    expect(calls[0].headers.get("last-event-id")).toBeNull();
    expect(calls[1].headers.get("last-event-id")).toBe("2");
    expect(calls[0].url).toBe("/api/sessions/9/events");
    expect(calls[0].url).not.toMatch(/token/i);
  });

  it("starts from a persisted cursor so a reload does not replay what was applied", async () => {
    const seen: number[] = [];
    const { calls } = stubFetch([
      () => streamResponse([envelopeFrame(7), closedFrame("access_revoked")]),
    ]);
    const channel = createSessionChannel(
      { sessionId: 2, initialSeq: 6, retryMinMs: 1 },
      { onEvent: (e) => seen.push(e.seq) },
    );
    await channel.done;
    expect(calls[0].headers.get("last-event-id")).toBe("6");
    expect(seen).toEqual([7]);
  });

  it("treats access_revoked as terminal and makes no further request", async () => {
    const statuses: ChannelStatus[] = [];
    const { fn } = stubFetch([() => streamResponse([closedFrame("access_revoked")])]);
    const channel = createSessionChannel(
      { sessionId: 1, retryMinMs: 1 },
      { onEvent: () => {}, onStatus: (s) => statuses.push(s) },
    );
    await channel.done;
    expect(fn).toHaveBeenCalledTimes(1);
    expect(statuses[statuses.length - 1]).toBe("closed");
  });

  it("treats a refused subscription as terminal, whatever the status code", async () => {
    for (const status of [401, 403, 404]) {
      const { fn } = stubFetch([() => new Response("{}", { status })]);
      const channel = createSessionChannel({ sessionId: 1, retryMinMs: 1 }, { onEvent: () => {} });
      await channel.done;
      expect(fn).toHaveBeenCalledTimes(1);
      vi.unstubAllGlobals();
    }
  });

  it("retries a transient server error with a growing delay", async () => {
    const { fn } = stubFetch([
      () => new Response("{}", { status: 503 }),
      () => new Response("{}", { status: 503 }),
      () => streamResponse([closedFrame("access_revoked")]),
    ]);
    const channel = createSessionChannel({ sessionId: 1, retryMinMs: 1, retryMaxMs: 4 }, { onEvent: () => {} });
    await channel.done;
    expect(fn).toHaveBeenCalledTimes(3);
  });

  it("hands a resync snapshot to onSnapshot and moves the cursor to its head", async () => {
    const events: number[] = [];
    const snapshots: number[] = [];
    stubFetch([
      () =>
        streamResponse([
          `id: 40\nevent: snapshot\ndata: ${JSON.stringify({ seq: 40, type: "snapshot", channel: "system", actor: null, turn_id: null, at: "x", data: { presence: [] } })}\n\n`,
          envelopeFrame(41),
          closedFrame("access_revoked"),
        ]),
    ]);
    const channel = createSessionChannel(
      { sessionId: 1, retryMinMs: 1 },
      {
        onEvent: (e) => events.push(e.seq),
        onSnapshot: (e) => snapshots.push(e.seq),
      },
    );
    await channel.done;
    expect(snapshots).toEqual([40]);
    expect(events).toEqual([41]);
    expect(channel.lastSeq()).toBe(41);
  });

  it("stop aborts an open subscription and resolves done", async () => {
    stubFetch([
      (init) => streamResponse([envelopeFrame(1)], { hold: true, signal: init.signal ?? undefined }),
    ]);
    const events: number[] = [];
    const channel = createSessionChannel({ sessionId: 1, retryMinMs: 1 }, { onEvent: (e) => events.push(e.seq) });
    await new Promise((resolve) => setTimeout(resolve, 20));
    channel.stop();
    await channel.done;
    expect(events).toEqual([1]);
  });
});
