/**
 * Fake HTTP/SSE transport for mounted integration tests.
 *
 * Every request is recorded with its AbortSignal. A responder either returns a Response (or a
 * promise of one) or `HOLD`, which leaves the request in flight until its signal aborts, the
 * way a real fetch behaves. Unmatched requests are held too, so an unexpected call is visible
 * in `calls` instead of failing silently.
 */
import { vi } from "vitest";

export const HOLD = Symbol("hold");

export interface FakeCall {
  method: string;
  path: string;
  body: unknown;
  signal: AbortSignal | null;
  /** True when the request was left in flight (HOLD) rather than answered. */
  held: boolean;
}

export type Responder = (call: FakeCall) => Response | Promise<Response> | typeof HOLD;

export function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

/** A held request rejects with AbortError when its signal fires, like a real fetch. */
function holdUntilAbort(signal: AbortSignal | null): Promise<never> {
  return new Promise<never>((_, reject) => {
    signal?.addEventListener("abort", () => reject(new DOMException("The operation was aborted.", "AbortError")), { once: true });
  });
}

/**
 * An SSE response from fixed frames. With `hold`, the stream stays open after the frames and
 * errors with AbortError when the request signal fires (a stalled provider).
 */
export function sseResponse(frames: Array<[event: string, data: unknown]>, signal: AbortSignal | null, hold = false): Response {
  const encoder = new TextEncoder();
  let controller!: ReadableStreamDefaultController<Uint8Array>;
  const stream = new ReadableStream<Uint8Array>({
    start(c) {
      controller = c;
      for (const [event, data] of frames) {
        c.enqueue(encoder.encode(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`));
      }
      if (!hold) c.close();
    },
  });
  if (hold) {
    signal?.addEventListener("abort", () => {
      try { controller.error(new DOMException("The operation was aborted.", "AbortError")); } catch { /* already closed */ }
    }, { once: true });
  }
  return new Response(stream, { headers: { "content-type": "text/event-stream" } });
}

/** Install the fake as global fetch. Responders match on "METHOD /pathname" (first match wins). */
export function installFakeApi(responders: Array<[pattern: RegExp, respond: Responder]>) {
  const calls: FakeCall[] = [];
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), "http://localhost");
    const method = (init?.method ?? "GET").toUpperCase();
    const signal = init?.signal ?? null;
    const body = typeof init?.body === "string" ? JSON.parse(init.body) as unknown : null;
    const key = `${method} ${url.pathname}`;
    const call: FakeCall = { method, path: url.pathname + url.search, body, signal, held: false };
    calls.push(call);
    const match = responders.find(([pattern]) => pattern.test(key));
    const result = match ? match[1](call) : HOLD;
    if (result === HOLD) {
      call.held = true;
      return holdUntilAbort(signal);
    }
    return Promise.resolve(result);
  });
  vi.stubGlobal("fetch", fetchMock);
  return { calls, fetchMock, callsTo: (pattern: RegExp) => calls.filter((c) => pattern.test(`${c.method} ${c.path.split("?")[0]}`)) };
}
