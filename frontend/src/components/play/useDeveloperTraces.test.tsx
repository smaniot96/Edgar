// @vitest-environment jsdom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, listSessionTurnTraces } from "../../api/client";
import type { TurnTraceList } from "../../api/types";
import type { TurnAction } from "./turnState";
import { useDeveloperTraces } from "./useDeveloperTraces";

vi.mock("../../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api/client")>();
  return { ...actual, listSessionTurnTraces: vi.fn() };
});

vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);

const roots: ReturnType<typeof createRoot>[] = [];
const list = vi.mocked(listSessionTurnTraces);
const BOOT = { history: [] };

function mount(enabled: boolean, dispatch: (action: TurnAction) => void, boot: typeof BOOT | null = BOOT) {
  function Probe() {
    useDeveloperTraces(7, enabled, boot, dispatch);
    return null;
  }
  const host = document.createElement("div");
  const root = createRoot(host);
  roots.push(root);
  act(() => root.render(<Probe />));
  return root;
}

async function flush() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

beforeEach(() => {
  list.mockReset();
});

afterEach(() => {
  roots.splice(0).forEach((root) => act(() => root.unmount()));
});

describe("useDeveloperTraces", () => {
  it("never requests traces in Player mode", async () => {
    const dispatch = vi.fn();
    mount(false, dispatch);
    await flush();
    expect(list).not.toHaveBeenCalled();
    expect(dispatch).not.toHaveBeenCalled();
  });

  it("waits for the history seed before requesting, then attaches the loaded list", async () => {
    const dispatch = vi.fn();
    const payload: TurnTraceList = { scope: "dm_turns", truncated: false, turns: [] };
    list.mockResolvedValue(payload);
    mount(true, dispatch, null);
    await flush();
    expect(list).not.toHaveBeenCalled();

    mount(true, dispatch, BOOT);
    await flush();
    expect(list).toHaveBeenCalledWith(7, expect.any(AbortSignal));
    expect(dispatch).toHaveBeenNthCalledWith(1, { type: "traces", lookup: { status: "loading" } });
    expect(dispatch).toHaveBeenLastCalledWith({
      type: "traces",
      lookup: { status: "loaded", records: [], truncated: false },
    });
  });

  it("maps a 404 to disabled and any other failure to failed", async () => {
    const dispatch = vi.fn();
    list.mockRejectedValueOnce(new ApiError(404, "{}"));
    mount(true, dispatch);
    await flush();
    expect(dispatch).toHaveBeenLastCalledWith({ type: "traces", lookup: { status: "disabled" } });

    dispatch.mockClear();
    list.mockRejectedValueOnce(new ApiError(500, "{}"));
    mount(true, dispatch);
    await flush();
    expect(dispatch).toHaveBeenLastCalledWith({ type: "traces", lookup: { status: "failed" } });
  });
});
