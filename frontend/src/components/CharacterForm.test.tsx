// @vitest-environment jsdom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { expect, it, vi } from "vitest";
import type { CharacterPreset } from "../api/types";
import { CharacterForm } from "./CharacterForm";

const api = vi.hoisted(() => ({ createCharacter: vi.fn(), listCharacterPresets: vi.fn() }));
vi.mock("../api/client", async () => ({ ...(await vi.importActual<object>("../api/client")), ...api }));
vi.mock("../dev/ModeContext", () => ({ useMode: () => ({ isDev: false }) }));

const fighter: CharacterPreset = {
  key: "fighter_l1", label: "Fighter (level 1)", character_class: "Fighter", level: 1, hp_max: 12, hp_current: 12,
  stats: { STR: 16, DEX: 12, CON: 14, INT: 10, WIS: 12, CHA: 8 },
  inventory: { weapons: ["longsword", "shield"], armor: "chain mail" },
  engine_support: "Engine: Second Wind; Action Surge from level 2",
};
const wizard: CharacterPreset = {
  key: "wizard_l1", label: "Wizard (level 1)", character_class: "Wizard", level: 1, hp_max: 6, hp_current: 6,
  stats: { STR: 8, DEX: 14, CON: 12, INT: 17, WIS: 13, CHA: 10 },
  inventory: { weapons: ["quarterstaff", "dagger"], armor: "none", spellbook: true },
  engine_support: "Engine: catalog spells only",
};

async function renderForm(props: Partial<Parameters<typeof CharacterForm>[0]> = {}) {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  await act(async () => { root.render(<CharacterForm onCreated={vi.fn()} {...props} />); });
  const cleanup = async () => {
    await act(() => root.unmount());
    container.remove();
  };
  return { container, cleanup };
}

function presetButton(container: HTMLElement, label: string): HTMLButtonElement {
  const button = [...container.querySelectorAll("button")].find((b) => b.textContent?.includes(label));
  if (!button) throw new Error(`no preset button for ${label}`);
  return button;
}

async function submitForm(container: HTMLElement) {
  await act(async () => { container.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })); });
}

it("reuses the same body and request ID after an ambiguous standalone hero response", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  api.listCharacterPresets.mockResolvedValue([{ ...fighter, key: "ranger_l1", label: "Ranger (level 1)", character_class: "Ranger", hp_max: 10, hp_current: 10, stats: { STR: 12, DEX: 16, CON: 13, INT: 10, WIS: 14, CHA: 10 }, inventory: { weapons: ["longbow", "shortsword"], armor: "leather", arrows: 20 } }]);
  api.createCharacter.mockRejectedValueOnce(new Error("response lost"));
  api.createCharacter.mockResolvedValueOnce({ id: 44, name: "Mara" });
  const created = vi.fn();
  const { container, cleanup } = await renderForm({ onCreated: created, initialDraft: { name: "Mara", presetKey: null, characterClass: "Ranger", hpText: "12" } });
  try {
    await submitForm(container);
    expect(container.querySelector<HTMLInputElement>("input")!.disabled).toBe(true);
    expect(container.textContent).toContain("Retry saved hero creation");
    await submitForm(container);
    expect(api.createCharacter).toHaveBeenCalledTimes(2);
    expect(api.createCharacter.mock.calls[1]).toEqual(api.createCharacter.mock.calls[0]);
    expect(api.createCharacter.mock.calls[0][1]).toMatch(/^[a-f0-9-]{36}$/);
    expect(created).toHaveBeenCalledWith({ id: 44, name: "Mara" });
  } finally {
    await cleanup();
    vi.unstubAllGlobals();
  }
});

it("offers the supported presets as the class picker, with no free-text class field", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  api.listCharacterPresets.mockResolvedValue([fighter, wizard]);
  const { container, cleanup } = await renderForm();
  try {
    // Only the name and HP fields are typed inputs; the class is picked, never typed.
    expect(container.querySelectorAll("input")).toHaveLength(2);
    expect(presetButton(container, "Fighter").getAttribute("aria-pressed")).toBe("true");
    expect(container.textContent).toContain("Engine: catalog spells only");
    expect(container.textContent).toContain("Kit: quarterstaff, dagger, spellbook");
  } finally {
    await cleanup();
    vi.unstubAllGlobals();
  }
});

it("picking another class resets HP and submits that preset's class, stats and kit", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  api.listCharacterPresets.mockResolvedValue([fighter, wizard]);
  api.createCharacter.mockReset();
  api.createCharacter.mockResolvedValue({ id: 45, name: "Sable" });
  const { container, cleanup } = await renderForm({ initialDraft: { name: "Sable", presetKey: null, characterClass: null, hpText: "20" } });
  try {
    await act(async () => { presetButton(container, "Wizard").click(); });
    expect(container.querySelector<HTMLInputElement>('input[type="number"]')!.value).toBe("6");
    await submitForm(container);
    expect(api.createCharacter).toHaveBeenCalledWith(
      {
        name: "Sable",
        character_class: "Wizard",
        level: 1,
        hp_max: 6,
        base_stats: wizard.stats,
        base_inventory: wizard.inventory,
      },
      expect.any(String),
    );
  } finally {
    await cleanup();
    vi.unstubAllGlobals();
  }
});

it("never saves an unsupported draft class with another preset's kit", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  api.listCharacterPresets.mockResolvedValue([fighter, wizard]);
  api.createCharacter.mockReset();
  api.createCharacter.mockResolvedValue({ id: 46, name: "Lute" });
  // A draft saved before the picker existed may still say "Bard" with a Wizard preset picked.
  const { container, cleanup } = await renderForm({ initialDraft: { name: "Lute", presetKey: "wizard_l1", characterClass: "Bard", hpText: "8" } });
  try {
    expect(container.textContent).not.toContain("Bard");
    await submitForm(container);
    const body = api.createCharacter.mock.calls[0][0];
    expect(body.character_class).toBe("Wizard");
    expect(body.base_inventory).toEqual(wizard.inventory);
  } finally {
    await cleanup();
    vi.unstubAllGlobals();
  }
});

it("changing starting level submits the returned HP and improved abilities, and retains that level in the draft", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  const advanced = { ...fighter, key: "fighter_l4", label: "Fighter (level 4)", level: 4, hp_max: 36, hp_current: 36, stats: { ...fighter.stats, STR: 18 } };
  api.listCharacterPresets.mockImplementation(async (_signal, level: number) => level === 4 ? [advanced] : [fighter]);
  api.createCharacter.mockReset();
  api.createCharacter.mockResolvedValue({ id: 51, name: "Mira" });
  const onDraftChange = vi.fn();
  const { container, cleanup } = await renderForm({ initialDraft: { name: "Mira", presetKey: "fighter_l1", characterClass: "Fighter", hpText: "44" }, onDraftChange });
  try {
    await act(async () => {
      const select = container.querySelector<HTMLSelectElement>('select[aria-label="Starting level"]')!;
      select.value = "4";
      select.dispatchEvent(new Event("change", { bubbles: true }));
    });
    expect(container.querySelector<HTMLInputElement>('input[type="number"]')!.value).toBe("36");
    expect(onDraftChange).toHaveBeenLastCalledWith(expect.objectContaining({ presetKey: "fighter_l4", hpText: null }));
    await submitForm(container);
    expect(api.createCharacter).toHaveBeenCalledWith(expect.objectContaining({ level: 4, hp_max: 36, base_stats: advanced.stats }), expect.any(String));
  } finally { await cleanup(); vi.unstubAllGlobals(); }
});
