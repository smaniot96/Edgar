// @vitest-environment jsdom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { beforeEach, expect, it, vi } from "vitest";

import { getSessionMap } from "../../api/client";
import type { SessionMapRead } from "../../api/types";
import { useSessionMap } from "./useSessionMap";

vi.mock("../../api/client", () => ({ getSessionMap: vi.fn() }));

function mapFor(sessionId: number, sourceVersion: string): SessionMapRead {
  return {
    session_id: sessionId,
    source_version: sourceVersion,
    topology: "prepared",
    schematic: true,
    scale: null,
    current_scene_id: null,
    places: [],
    routes: [],
    note: null,
    journal_entry_ids: {},
    known_place_entries: [],
    artwork: [],
    tracking: "available",
  };
}

function Probe({ sessionId }: { sessionId: number }) {
  const { data, loading } = useSessionMap(sessionId, true);
  return <output>{loading ? "loading" : data ? `${data.session_id}:${data.source_version}` : "empty"}</output>;
}

beforeEach(() => vi.clearAllMocks());

it("drops aborted map responses and never shows the prior session's data", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  const requests: Array<{
    sessionId: number;
    signal: AbortSignal | undefined;
    resolve: (value: SessionMapRead) => void;
  }> = [];
  vi.mocked(getSessionMap).mockImplementation((sessionId, signal) => new Promise((resolve) => {
    requests.push({ sessionId: Number(sessionId), signal, resolve });
  }));

  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  try {
    await act(() => root.render(<Probe sessionId={1} />));
    await act(() => root.render(<Probe sessionId={2} />));
    expect(requests).toHaveLength(2);
    expect(requests[0].signal?.aborted).toBe(true);
    expect(container.textContent).toBe("loading");

    await act(async () => {
      requests[0].resolve(mapFor(1, "old-version"));
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    expect(container.textContent).toBe("loading");

    await act(async () => {
      requests[1].resolve(mapFor(2, "new-version"));
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    expect(container.textContent).toBe("2:new-version");
  } finally {
    await act(() => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
  }
});
