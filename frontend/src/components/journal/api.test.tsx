// @vitest-environment jsdom
import { act, useEffect } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";

const apiFetchMock = vi.hoisted(() => vi.fn());
vi.mock("../../api/client", () => ({ apiFetch: apiFetchMock }));

import { useJournal } from "./api";

function payload(sessionId: number) {
  return {
    session_id: sessionId,
    tracking: "available" as const,
    entries: [],
    current_place: null,
    active_goal: null,
  };
}

function Probe({ sessionId, onRefresh }: { sessionId: number | null; onRefresh: (refresh: () => void) => void }) {
  const state = useJournal(sessionId);
  useEffect(() => onRefresh(state.refresh), [onRefresh, state.refresh]);
  return (
    <output>
      {JSON.stringify({
        sessionId: state.data?.session_id ?? null,
        loading: state.loading,
        stale: state.stale,
        error: Boolean(state.error),
      })}
    </output>
  );
}

async function flush() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

let root: Root | undefined;
let container: HTMLDivElement | undefined;
afterEach(async () => {
  if (root) await act(() => root!.unmount());
  root = undefined;
  container?.remove();
  container = undefined;
  apiFetchMock.mockReset();
  vi.unstubAllGlobals();
});

it("keeps the last good same-session journal on refresh errors and drops it on session changes", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  apiFetchMock
    .mockResolvedValueOnce({ json: async () => payload(1) })
    .mockRejectedValueOnce(new Error("PRIVATE API BODY"))
    .mockResolvedValueOnce({ json: async () => payload(2) });
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  let refresh: (() => void) | undefined;
  const updateRefresh = (next: () => void) => { refresh = next; };

  await act(() => root!.render(<Probe sessionId={1} onRefresh={updateRefresh} />));
  await flush();
  expect(container.textContent).toContain('"sessionId":1');
  expect(container.textContent).toContain('"stale":false');

  await act(() => refresh?.());
  await flush();
  expect(container.textContent).toContain('"sessionId":1');
  expect(container.textContent).toContain('"stale":true');
  expect(container.textContent).toContain('"error":true');
  expect(container.textContent).not.toContain("PRIVATE API BODY");

  await act(() => root!.render(<Probe sessionId={2} onRefresh={updateRefresh} />));
  await flush();
  expect(container.textContent).toContain('"sessionId":2');
  expect(container.textContent).not.toContain('"sessionId":1');
  expect(container.textContent).toContain('"stale":false');
});

it("ignores an older session response even when its JSON resolves after a session switch", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  let resolveOld!: (value: ReturnType<typeof payload>) => void;
  apiFetchMock
    .mockResolvedValueOnce({ json: () => new Promise((resolve) => { resolveOld = resolve; }) })
    .mockResolvedValueOnce({ json: async () => payload(2) });
  container = document.createElement("div"); document.body.append(container);
  root = createRoot(container);
  const updateRefresh = () => {};
  await act(() => root!.render(<Probe sessionId={1} onRefresh={updateRefresh} />));
  await flush();
  await act(() => root!.render(<Probe sessionId={2} onRefresh={updateRefresh} />));
  await flush();
  expect(container.textContent).toContain('"sessionId":2');
  await act(async () => { resolveOld(payload(1)); });
  expect(container.textContent).toContain('"sessionId":2');
  expect(container.textContent).not.toContain('"sessionId":1');
});
