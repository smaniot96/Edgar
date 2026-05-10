export async function* readSse(res: Response): AsyncGenerator<{
  event: string;
  data: Record<string, unknown>;
}> {
  const reader = res.body!.getReader();
  const dec = new TextDecoder();
  let buf = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) return;
    buf += dec.decode(value, { stream: true });
    let i: number;
    while ((i = buf.indexOf("\n\n")) >= 0) {
      const frame = buf.slice(0, i);
      buf = buf.slice(i + 2);
      let event = "";
      let data = "";
      for (const ln of frame.split("\n")) {
        if (ln.startsWith("event:")) event = ln.slice(6).trim();
        else if (ln.startsWith("data:")) data += ln.slice(5).trim();
      }
      let parsed: Record<string, unknown> = {};
      if (data) {
        try {
          parsed = JSON.parse(data) as Record<string, unknown>;
        } catch {
          parsed = {};
        }
      }
      yield { event, data: parsed };
    }
  }
}
