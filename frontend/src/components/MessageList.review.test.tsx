// @vitest-environment jsdom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { expect, it, vi } from "vitest";
import { MessageList } from "./MessageList";
vi.mock("../dev/ModeContext", () => ({ useMode: () => ({ isDev: false }) }));

it("leaves the persisted reading anchor untouched until boot history is seeded", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  const container = document.createElement("div"); document.body.append(container);
  const root = createRoot(container);
  const key = "review-anchor";
  const saved = JSON.stringify({ messageId: "old", eventId: 3, offset: -20, scrollTop: 513.5 });
  sessionStorage.setItem(key, saved);
  try {
    await act(() => root.render(<MessageList messages={[]} pending={null} storageKey={key} restoreReady={false} />));
    expect(sessionStorage.getItem(key)).toBe(saved);
    await act(() => container.querySelector('[role="log"]')!.dispatchEvent(new Event("scroll", { bubbles: true })));
    expect(sessionStorage.getItem(key)).toBe(saved);
  } finally {
    await act(() => root.unmount()); container.remove(); sessionStorage.removeItem(key); vi.unstubAllGlobals();
  }
});
