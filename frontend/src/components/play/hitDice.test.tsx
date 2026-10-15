// @vitest-environment jsdom
import { act, type ReactElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";

import type { AdjudicationResult, RestRecord } from "../../api/types";
import { CharacterSheet } from "./CharacterSheet";
import { RollCard } from "./RollCard";
import type { SheetCharacter } from "./gameState";

const rest: RestRecord = {
  kind: "short",
  refused: false,
  expression: "1d10+2",
  rolled: 7,
  healed: 7,
  die: "d10",
  hit_dice_remaining: 0,
  hit_dice_total: 1,
  hit_dice_recovered: null,
};

function ruling(extra: Partial<AdjudicationResult>): AdjudicationResult {
  return { success: true, damage: null, dice_result: null, mechanical_summary: "", ...extra };
}

const sheet: SheetCharacter = {
  id: 1,
  name: "Eda",
  className: "Fighter",
  level: 1,
  hpCurrent: 12,
  hpMax: 12,
  ac: null,
  abilities: [],
  conditions: [],
  inventory: [],
  secondWind: null,
  spellSlots: null,
  hitDice: { remaining: 0, total: 1, die: "d10" },
};

describe("Hit Dice display (EDG-026)", () => {
  let container: HTMLDivElement | null = null;
  let root: Root | null = null;

  async function render(node: ReactElement) {
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    await act(() => root!.render(node));
    return container;
  }

  afterEach(async () => {
    if (root) await act(() => root!.unmount());
    container?.remove();
    root = null;
    container = null;
  });

  it("shows a rest turn's evidence even though it rolled no check", async () => {
    const el = await render(<RollCard adj={ruling({ rest_record: rest })} />);
    const text = "Short rest · 1d10+2 = 7 · healed 7 HP · Hit Dice 0/1 d10 left";
    expect(el.textContent).toContain(text);
    expect(el.querySelector(`[aria-label="${text}"]`)).not.toBeNull();
  });

  it("renders nothing for a turn with neither dice nor rest evidence", async () => {
    const el = await render(<RollCard adj={ruling({})} />);
    expect(el.innerHTML).toBe("");
  });

  it("shows the remaining Hit Dice on the sheet, and nothing when the server reported none", async () => {
    const el = await render(<CharacterSheet character={sheet} open onClose={() => undefined} />);
    expect(el.textContent).toContain("Hit Dice");
    expect(el.textContent).toContain("0/1 d10");

    await act(() => root!.render(<CharacterSheet character={{ ...sheet, hitDice: null }} open onClose={() => undefined} />));
    expect(el.textContent).not.toContain("Hit Dice");
  });
});
