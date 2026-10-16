// @vitest-environment jsdom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { expect, it, vi } from "vitest";
import { ComposerBar } from "./ComposerBar";

it("keeps Enter explicit and guards multiline, composition, uncertain and busy sends", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  const send = vi.fn(); const change = vi.fn(); const stop = vi.fn();
  const container = document.createElement("div"); document.body.append(container); const root = createRoot(container);
  const render = (busy = false, allowSend = true) => root.render(<ComposerBar value={'Keep the note dry.\nAsk Mara.'} onChange={change} onSend={send} onStop={stop} busy={busy} allowSend={allowSend} />);
  try {
    await act(() => render());
    const textarea = container.querySelector("textarea")!;
    const key = (options: KeyboardEventInit = {}) => textarea.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true, ...options }));
    await act(() => { key({ shiftKey: true }); key({ isComposing: true }); key({ keyCode: 229 }); });
    expect(send).not.toHaveBeenCalled();
    await act(() => { key(); }); expect(send).toHaveBeenCalledOnce();
    await act(() => render(false, false));
    await act(() => { key(); }); expect(send).toHaveBeenCalledOnce();
    await act(() => render(true));
    expect(textarea.disabled).toBe(false);
    await act(() => { key(); }); expect(send).toHaveBeenCalledOnce();
    await act(() => container.querySelector<HTMLButtonElement>('button:not([disabled])')!.click());
    expect(stop).toHaveBeenCalledOnce();
    expect(textarea.value).toBe('Keep the note dry.\nAsk Mara.');
  } finally { await act(() => root.unmount()); container.remove(); vi.unstubAllGlobals(); }
});

it("grows for multiline drafts, caps its height and returns to one-line height when cleared", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  const container = document.createElement("div"); document.body.append(container); const root = createRoot(container);
  const render = (value: string) => root.render(<ComposerBar value={value} onChange={vi.fn()} onSend={vi.fn()} busy={false} />);
  try {
    await act(() => render("One line"));
    const textarea = container.querySelector<HTMLTextAreaElement>("textarea")!;
    Object.defineProperty(textarea, "scrollHeight", { configurable: true, get: () => 120 });
    await act(() => render("A multiline note\nWith another line"));
    expect(textarea.style.height).toBe("120px");
    expect(textarea.style.overflowY).toBe("hidden");

    Object.defineProperty(textarea, "scrollHeight", { configurable: true, get: () => 300 });
    await act(() => render("A long note\n".repeat(20)));
    expect(textarea.style.height).toBe("160px");
    expect(textarea.style.overflowY).toBe("auto");

    Object.defineProperty(textarea, "scrollHeight", { configurable: true, get: () => 24 });
    await act(() => render(""));
    expect(textarea.style.height).toBe("44px");
    expect(textarea.style.overflowY).toBe("hidden");
  } finally { await act(() => root.unmount()); container.remove(); vi.unstubAllGlobals(); }
});
