/**
 * Minimal Server-Sent Events parser for `fetch` response bodies.
 *
 * `EventSource` can't POST, so the turn stream is read with fetch + this parser. It follows
 * the WHATWG framing rules that matter here: frames end at a blank line, lines may end in
 * `\n`, `\r\n` or `\r`, `data:` lines are joined with `\n`, and `:` comment lines (keep-alive
 * pings) are ignored. A trailing frame without a final blank line is flushed at end of stream.
 */

export interface SseEvent {
  event: string;
  data: Record<string, unknown>;
}

/** Parse one raw frame body (already split from the stream). Returns null for empty frames. */
function parseFrame(lines: string[]): SseEvent | null {
  let event = "";
  const dataLines: string[] = [];
  let sawField = false;
  for (const ln of lines) {
    if (ln === "" || ln.startsWith(":")) continue;
    const colon = ln.indexOf(":");
    const field = colon >= 0 ? ln.slice(0, colon) : ln;
    let value = colon >= 0 ? ln.slice(colon + 1) : "";
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "event") {
      event = value.trim();
      sawField = true;
    } else if (field === "data") {
      dataLines.push(value);
      sawField = true;
    }
  }
  if (!sawField) return null;
  const raw = dataLines.join("\n");
  let data: Record<string, unknown> = {};
  if (raw.trim()) {
    try {
      const parsed: unknown = JSON.parse(raw);
      data =
        parsed && typeof parsed === "object" && !Array.isArray(parsed)
          ? (parsed as Record<string, unknown>)
          : { value: parsed };
    } catch {
      data = { raw };
    }
  }
  return { event: event || "message", data };
}

/**
 * Incremental parser: `push` decoded text as it arrives and get back every complete event;
 * call `flush` at end of stream to recover a final frame that lacked its blank-line terminator.
 */
export function createSseParser() {
  let buf = "";
  /** The previous chunk ended in "\r": a "\n" at the start of the next chunk belongs to it. */
  let pendingCr = false;
  let frame: string[] = [];

  function drain(): SseEvent[] {
    const out: SseEvent[] = [];
    let nl: number;
    while ((nl = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, nl);
      buf = buf.slice(nl + 1);
      if (line === "") {
        const ev = parseFrame(frame);
        frame = [];
        if (ev) out.push(ev);
      } else {
        frame.push(line);
      }
    }
    return out;
  }

  return {
    push(text: string): SseEvent[] {
      if (pendingCr && text.startsWith("\n")) text = text.slice(1);
      pendingCr = text.endsWith("\r");
      buf += text.replace(/\r\n?/g, "\n");
      return drain();
    },
    flush(): SseEvent[] {
      const out = drain();
      if (buf.length > 0) {
        frame.push(buf);
        buf = "";
      }
      if (frame.length) {
        const ev = parseFrame(frame);
        frame = [];
        if (ev) out.push(ev);
      }
      pendingCr = false;
      return out;
    },
  };
}

export interface ReadSseOptions {
  /** Called for every chunk read from the network (including keep-alive comments). */
  onActivity?: () => void;
}

/** Iterate SSE events from a fetch Response. Throws if the response has no body. */
export async function* readSse(
  res: Response,
  opts: ReadSseOptions = {},
): AsyncGenerator<SseEvent> {
  if (!res.body) throw new Error("The server response had no body to stream.");
  const reader = res.body.getReader();
  const dec = new TextDecoder();
  const parser = createSseParser();
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      opts.onActivity?.();
      for (const ev of parser.push(dec.decode(value, { stream: true }))) yield ev;
    }
    const tail = dec.decode();
    if (tail) for (const ev of parser.push(tail)) yield ev;
    for (const ev of parser.flush()) yield ev;
  } finally {
    // Don't cancel after a terminal event: the server still has to release its turn lock in
    // the generator's `finally`, and a client disconnect could interrupt that. Aborts are
    // handled by the fetch AbortController.
    try {
      reader.releaseLock();
    } catch {
      /* already released / errored */
    }
  }
}
