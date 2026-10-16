import { describe, expect, it } from "vitest";

import { consumeTurnStream, parseTurnError } from "./turnStream";
import { createSseParser, readSse } from "./sse";

function streamResponse(chunks: string[]): Response {
  const enc = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const c of chunks) controller.enqueue(enc.encode(c));
      controller.close();
    },
  });
  return new Response(body, { headers: { "content-type": "text/event-stream" } });
}

async function collect(res: Response) {
  const out = [];
  for await (const ev of readSse(res)) out.push(ev);
  return out;
}

describe("createSseParser", () => {
  it("parses an event split across many chunks", () => {
    const p = createSseParser();
    const frame = 'event: token\ndata: {"text":"Hello"}\n\n';
    const events = [];
    for (const ch of frame) events.push(...p.push(ch));
    expect(events).toEqual([{ event: "token", data: { text: "Hello" } }]);
  });

  it("handles several events in one chunk", () => {
    const p = createSseParser();
    const events = p.push(
      'event: status\ndata: {"stage":"parsing"}\n\nevent: token\ndata: {"text":"a"}\n\n',
    );
    expect(events.map((e) => e.event)).toEqual(["status", "token"]);
  });

  it("accepts \\r\\n and bare \\r line endings, including \\r\\n split across chunks", () => {
    const p = createSseParser();
    const a = p.push('event: token\r\ndata: {"text":"x"}\r');
    const b = p.push('\n\r\nevent: done\rdata: {}\r\r');
    expect([...a, ...b]).toEqual([
      { event: "token", data: { text: "x" } },
      { event: "done", data: {} },
    ]);
  });

  it("ignores keep-alive comments and joins multi-line data", () => {
    const p = createSseParser();
    const events = p.push(': ping\n\nevent: token\ndata: {"text":\ndata: "hi"}\n\n');
    expect(events).toEqual([{ event: "token", data: { text: "hi" } }]);
  });

  it("flushes a final frame that lacks the blank-line terminator", () => {
    const p = createSseParser();
    expect(p.push('event: done\ndata: {"narration":"end"}')).toEqual([]);
    expect(p.flush()).toEqual([{ event: "done", data: { narration: "end" } }]);
  });

  it("keeps malformed JSON as raw text instead of dropping it", () => {
    const p = createSseParser();
    expect(p.push("event: error\ndata: boom\n\n")).toEqual([{ event: "error", data: { raw: "boom" } }]);
  });
});

describe("readSse", () => {
  it("yields events across network chunks and flushes the leftover buffer", async () => {
    const events = await collect(
      streamResponse(['event: tok', 'en\ndata: {"text":"a"}\n', '\nevent: done\ndata: {}']),
    );
    expect(events).toEqual([
      { event: "token", data: { text: "a" } },
      { event: "done", data: {} },
    ]);
  });

  it("throws when the response has no body", async () => {
    await expect(collect(new Response(null))).rejects.toThrow(/no body/);
  });
});

describe("consumeTurnStream", () => {
  it("returns done with the payload and forwards tokens", async () => {
    const tokens: string[] = [];
    const out = await consumeTurnStream(
      streamResponse([
        'event: status\ndata: {"stage":"narrating"}\n\n',
        'event: token\ndata: {"text":"Hi "}\n\nevent: token\ndata: {"text":"there"}\n\n',
        'event: done\ndata: {"narration":"Hi there","character":{"class":"Fighter"}}\n\n',
      ]),
      { onToken: (t) => tokens.push(t) },
    );
    expect(tokens.join("")).toBe("Hi there");
    expect(out).toEqual({
      kind: "done",
      result: { narration: "Hi there", character: { class: "Fighter" } },
    });
  });

  it("reports a premature end (no done/error) as incomplete", async () => {
    const out = await consumeTurnStream(
      streamResponse(['event: token\ndata: {"text":"partial"}\n\n']),
      {},
    );
    expect(out).toEqual({ kind: "incomplete" });
  });

  it("uses safe fallback copy for unrecognized error codes and details", async () => {
    const a = await consumeTurnStream(
      streamResponse(['event: error\ndata: {"code":"llm_timeout","message":"Too slow"}\n\n']),
      {},
    );
    expect(a).toMatchObject({ kind: "error", error: { code: "turn_failed", category: "internal" } });
    const b = await consumeTurnStream(streamResponse(['event: error\ndata: {"detail":"Nope"}\n\n']), {});
    expect(b).toMatchObject({ kind: "error", error: { code: "turn_failed", category: "internal" } });
  });

  it("signals activity for every chunk (watchdog feed)", async () => {
    let n = 0;
    await consumeTurnStream(streamResponse([": ping\n\n", ": ping\n\n", "event: done\ndata: {}\n\n"]), {
      onActivity: () => n++,
    });
    expect(n).toBe(3);
  });

  it("replaces plain-text error frames with safe fallback copy", async () => {
    const out = await consumeTurnStream(streamResponse(["event: error\ndata: Busy\n\n"]), {});
    expect(out).toMatchObject({ kind: "error", error: { message: "The turn could not be completed.", category: "internal" } });
  });
});

describe("parseTurnError", () => {
  it("uses safe fallback copy for unknown codes and free-form details", () => {
    expect(parseTurnError('{"detail":"Campaign has ended; reopen it to continue."}')).toMatchObject({
      code: "turn_failed", category: "internal",
    });
    expect(parseTurnError({ detail: { code: "turn_locked", message: "Busy" } })).toMatchObject({
      code: "turn_failed", category: "internal",
    });
    expect(parseTurnError("Bad Gateway")).toMatchObject({ code: "turn_failed", category: "internal" });
    expect(parseTurnError("", "fallback")).toMatchObject({ message: "fallback", code: "turn_failed" });
  });
});
