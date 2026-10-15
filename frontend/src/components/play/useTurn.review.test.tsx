// @vitest-environment jsdom
import { act, useEffect } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";
import { useTurn } from "./useTurn";

let cleanup: (() => Promise<void>) | undefined;
afterEach(async () => { await cleanup?.(); cleanup = undefined; vi.unstubAllGlobals(); });

it("ignores a receipt body that resolves after its session unmounts", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  let resolve!: (value: unknown) => void;
  const json = vi.fn(() => new Promise((done) => { resolve = done; }));
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ status: 200, json }));
  const onResult = vi.fn();
  const onAcknowledged = vi.fn();
  const container = document.createElement("div"); document.body.append(container);
  const root = createRoot(container);
  function Harness() {
    const turn = useTurn(1, { debug: false, onResult, onAcknowledged });
    return <button onClick={() => void turn.recover({ requestId: "pending-1", input: "Wait", draftRevision: 1 })}>Recover</button>;
  }
  await act(() => root.render(<Harness />));
  await act(async () => { container.querySelector("button")!.click(); });
  expect(json).toHaveBeenCalledOnce();
  await act(() => root.unmount()); container.remove();
  await act(async () => { resolve({ status: "committed", result: { event_id: 1, narration: "Saved" } }); });
  expect(onResult).not.toHaveBeenCalled();
  expect(onAcknowledged).not.toHaveBeenCalled();
});

it("acknowledges an already loaded receipt without replaying its old character state", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
    status: "committed", result: { event_id: 7, narration: "Original action.", character: { hp_current: 1 } },
  }))));
  const onResult = vi.fn(); const onAcknowledged = vi.fn();
  const container = document.createElement("div"); document.body.append(container);
  const root = createRoot(container);
  cleanup = async () => { await act(() => root.unmount()); container.remove(); };
  function Harness() {
    const turn = useTurn(1, { debug: false, onResult, onAcknowledged });
    const { dispatch } = turn;
    useEffect(() => { dispatch({ type: "reset", lines: [
      { id: "old", role: "dm", eventId: 7, content: "Original action." },
      { id: "new", role: "dm", eventId: 8, content: "A later turn changed your HP." },
    ] }); }, [dispatch]);
    return <><button onClick={() => void turn.recover({ requestId: "saved-7", input: "Wait", draftRevision: 3 })}>Recover</button>{turn.state.lines.map((line) => <p key={line.id}>{line.content}</p>)}</>;
  }
  await act(() => root.render(<Harness />));
  await act(async () => { container.querySelector("button")!.click(); });
  expect(onResult).not.toHaveBeenCalled();
  expect(onAcknowledged).toHaveBeenCalledWith(3, "saved-7");
  expect(container.querySelectorAll("p")).toHaveLength(2);
});
