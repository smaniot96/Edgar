// @vitest-environment jsdom
import { expect, it, vi } from "vitest";
import { afterBrowserPaint, beginBrowserTurn, browserTurnTimelines, markBrowserTurn } from "./browserTurnTimeline";

it("separates headers, received text, paint and readiness under one request with repeat-safe stages", () => {
  beginBrowserTurn(1, "timeline-1");
  markBrowserTurn("timeline-1", "headers");
  let row = browserTurnTimelines(1).find((item) => item.requestId === "timeline-1")!;
  expect(row.stages.headers).toBeGreaterThanOrEqual(0);
  expect(row.stages.first_text_paint).toBeUndefined();
  markBrowserTurn("timeline-1", "first_token");
  markBrowserTurn("timeline-1", "first_text_paint");
  markBrowserTurn("timeline-1", "composer_ready");
  const first = browserTurnTimelines(1).find((item) => item.requestId === "timeline-1")!.stages.first_text_paint;
  markBrowserTurn("timeline-1", "first_text_paint");
  row = browserTurnTimelines(1).find((item) => item.requestId === "timeline-1")!;
  expect(row.stages.first_text_paint).toBe(first);
  expect(row.stages.composer_ready).toBeGreaterThanOrEqual(first!);
  beginBrowserTurn(1, "timeline-1");
  expect(browserTurnTimelines(1).filter((item) => item.requestId === "timeline-1").map((item) => item.attempt)).toEqual([1, 2]);
  expect(browserTurnTimelines(1).at(-1)!.stages.first_text_paint).toBeUndefined();
});

it("observes paint only after two frames and cancels stale DOM observations", () => {
  const callbacks = new Map<number, FrameRequestCallback>(); let seq = 0;
  vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => { callbacks.set(++seq, callback); return seq; });
  vi.stubGlobal("cancelAnimationFrame", (id: number) => callbacks.delete(id));
  const visible = vi.spyOn(document, "visibilityState", "get").mockReturnValue("visible");
  const callback = vi.fn();
  try {
    const cancel = afterBrowserPaint(callback);
    callbacks.get(1)!(0); expect(callback).not.toHaveBeenCalled();
    cancel(); expect(callbacks.has(2)).toBe(false);
    afterBrowserPaint(callback); callbacks.get(3)!(1); callbacks.get(4)!(2);
    expect(callback).toHaveBeenCalledOnce();
    visible.mockReturnValue("hidden"); afterBrowserPaint(callback);
    expect(seq).toBe(4);
  } finally { vi.restoreAllMocks(); vi.unstubAllGlobals(); }
});
