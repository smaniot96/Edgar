// @vitest-environment jsdom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { expect, it, vi } from "vitest";
import { useTurn } from "./useTurn";

it("reports lost source truthfully and requests recovery state after an HTTP 409", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: {
    code: "campaign_sources_unavailable", message: "Recover the campaign adventure source or choose homebrew play.", retrieval_degraded: true,
  } }), { status: 409 })));
  const unavailable = vi.fn();
  const restored = vi.fn();
  const container = document.createElement("div"); document.body.append(container);
  const root = createRoot(container);
  function Harness() {
    const turn = useTurn(1, { debug: false, onSourcesUnavailable: unavailable, onFailed: restored });
    return <><button onClick={() => void turn.send("Explore")}>Explore</button>{turn.state.lines.map((line) => <p key={line.id}>{line.content}</p>)}</>;
  }
  try {
    await act(() => root.render(<Harness />));
    await act(async () => { container.querySelector("button")!.click(); });
    expect(container.textContent).toContain("Recover the campaign adventure source");
    expect(container.textContent).not.toContain("resolving a previous action");
    expect(unavailable).toHaveBeenCalledOnce(); expect(restored).toHaveBeenCalledWith("Explore", undefined);
  } finally {
    await act(() => root.unmount()); container.remove(); vi.unstubAllGlobals();
  }
});

it("requires an authoritative receipt check before retrying an ambiguous disconnect and reuses its ID", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  const fetchMock = vi.fn()
    .mockRejectedValueOnce(new TypeError("offline"))
    .mockResolvedValueOnce(new Response(null, { status: 404 }))
    .mockResolvedValueOnce(new Response(
      `event: done\ndata: ${JSON.stringify({ narration: "The gate opens." })}\n\n`,
      { headers: { "content-type": "text/event-stream" } },
    ));
  vi.stubGlobal("fetch", fetchMock);
  const onAcknowledged = vi.fn();
  const container = document.createElement("div"); document.body.append(container);
  const root = createRoot(container);
  function Harness() {
    const turn = useTurn(9, { debug: false, onAcknowledged });
    return <>
      <button onClick={() => void turn.send("Open the gate", 4)}>Send</button>
      <button onClick={() => {
        const line = turn.state.lines.find((entry) => entry.role === "error");
        if (line?.role === "error" && line.turnId && line.retryRequestId && line.retryInput) {
          turn.checkReceipt(line.turnId, line.retryRequestId, line.retryInput, line.draftRevision);
        }
      }}>Check save status</button>
      {turn.state.lines.map((line) => <p key={line.id}>{line.role === "error" ? `${line.content} ${line.uncertain ? "uncertain" : "retryable"}` : line.content}</p>)}
      <button onClick={() => {
        const line = turn.state.lines.find((entry) => entry.role === "error");
        if (line?.role === "error" && line.turnId && line.retryInput && line.retryRequestId) {
          turn.retry(line.turnId, line.retryInput, line.retryRequestId, line.draftRevision);
        }
      }}>Retry</button>
    </>;
  }
  try {
    await act(() => root.render(<Harness />));
    await act(async () => { container.querySelector("button")!.click(); });
    expect(container.textContent).toContain("uncertain");
    expect(JSON.parse(fetchMock.mock.calls[0][1].body as string).request_id).toMatch(/^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$/);
    await act(async () => { container.querySelectorAll("button")[1].click(); });
    expect(container.textContent).toContain("retryable");
    await act(async () => { container.querySelectorAll("button")[2].click(); });
    expect(JSON.parse(fetchMock.mock.calls[2][1].body as string).request_id)
      .toBe(JSON.parse(fetchMock.mock.calls[0][1].body as string).request_id);
    expect(onAcknowledged).toHaveBeenCalledWith(4, expect.any(String));
    expect(container.textContent).toContain("The gate opens.");
  } finally {
    await act(() => root.unmount()); container.remove(); vi.unstubAllGlobals();
  }
});

it("accepts a committed receipt after a lost stream and acknowledges only then", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  const result = { narration: "The door is open." };
  const fetchMock = vi.fn()
    .mockRejectedValueOnce(new TypeError("offline"))
    .mockResolvedValueOnce(new Response(JSON.stringify({ status: "committed", result }), { status: 200 }));
  vi.stubGlobal("fetch", fetchMock);
  const onAcknowledged = vi.fn();
  const onResult = vi.fn();
  const container = document.createElement("div"); document.body.append(container);
  const root = createRoot(container);
  function Harness() {
    const turn = useTurn(4, { debug: false, onAcknowledged, onResult });
    return <>
      <button onClick={() => void turn.send("Open it", 12)}>Send</button>
      <button onClick={() => {
        const line = turn.state.lines.find((entry) => entry.role === "error");
        if (line?.role === "error" && line.turnId && line.retryRequestId && line.retryInput) {
          turn.checkReceipt(line.turnId, line.retryRequestId, line.retryInput, line.draftRevision);
        }
      }}>Check save status</button>
      {turn.state.lines.map((line) => <p key={line.id}>{line.role === "error" ? line.content : line.content}</p>)}
    </>;
  }
  try {
    await act(() => root.render(<Harness />));
    await act(async () => { container.querySelectorAll("button")[0].click(); });
    expect(onAcknowledged).not.toHaveBeenCalled();
    await act(async () => { container.querySelectorAll("button")[1].click(); });
    expect(onAcknowledged).toHaveBeenCalledWith(12, expect.any(String));
    expect(onResult).toHaveBeenCalledWith(result);
    expect(container.textContent).toContain("The door is open.");
    expect(container.textContent).not.toContain("Check again before retrying");
  } finally {
    await act(() => root.unmount()); container.remove(); vi.unstubAllGlobals();
  }
});
