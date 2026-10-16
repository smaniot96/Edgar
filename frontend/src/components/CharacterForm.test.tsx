// @vitest-environment jsdom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { expect, it, vi } from "vitest";
import { CharacterForm } from "./CharacterForm";

const api = vi.hoisted(() => ({ createCharacter: vi.fn(), listCharacterPresets: vi.fn() }));
vi.mock("../api/client", async () => ({ ...(await vi.importActual<object>("../api/client")), ...api }));
vi.mock("../dev/ModeContext", () => ({ useMode: () => ({ isDev: false }) }));

it("reuses the same body and request ID after an ambiguous standalone hero response", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  api.listCharacterPresets.mockResolvedValue([]);
  api.createCharacter.mockRejectedValueOnce(new Error("response lost"));
  api.createCharacter.mockResolvedValueOnce({ id: 44, name: "Mara" });
  const created = vi.fn();
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  try {
    await act(async () => { root.render(<CharacterForm onCreated={created} initialDraft={{ name: "Mara", presetKey: null, characterClass: "Ranger", hpText: "12" }} />); });
    await act(async () => { container.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })); });
    expect(container.querySelector<HTMLInputElement>("input")!.disabled).toBe(true);
    expect(container.textContent).toContain("Retry saved hero creation");
    await act(async () => { container.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })); });
    expect(api.createCharacter).toHaveBeenCalledTimes(2);
    expect(api.createCharacter.mock.calls[1]).toEqual(api.createCharacter.mock.calls[0]);
    expect(api.createCharacter.mock.calls[0][1]).toMatch(/^[a-f0-9-]{36}$/);
    expect(created).toHaveBeenCalledWith({ id: 44, name: "Mara" });
  } finally {
    await act(() => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
  }
});
