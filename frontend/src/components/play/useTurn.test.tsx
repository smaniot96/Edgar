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
    expect(unavailable).toHaveBeenCalledOnce(); expect(restored).toHaveBeenCalledWith("Explore");
  } finally {
    await act(() => root.unmount()); container.remove(); vi.unstubAllGlobals();
  }
});
