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
