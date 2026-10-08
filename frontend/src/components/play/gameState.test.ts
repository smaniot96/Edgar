import { describe, expect, it } from "vitest";

import type { CharacterRead } from "../../api/types";
import { mergeTurnCharacter, orderCombatants, parseInventory, sheetFromLibrary } from "./gameState";
import { rollParts, rollSummary } from "./rollText";

const eda: CharacterRead = {
  id: 1,
  owner_user_id: 1,
  name: "Eda",
  character_class: "Fighter",
  level: 1,
  hp_max: 12,
  base_stats: { CHA: 8, CON: 14, DEX: 12, INT: 10, STR: 16, WIS: 12 },
  base_inventory: { armor: "chain shirt", weapons: ["longsword"] },
  created_at: "",
};

describe("character sheet", () => {
  it("builds a sheet with abilities in STR..CHA order and grouped inventory", () => {
    const s = sheetFromLibrary(eda);
    expect(s.abilities.map((a) => a.key)).toEqual(["STR", "DEX", "CON", "INT", "WIS", "CHA"]);
    expect(s.inventory).toEqual([
      { label: "Armor", items: ["chain shirt"] },
      { label: "Weapons", items: ["longsword"] },
    ]);
    expect(s.hpCurrent).toBe(12);
  });

  it("merges the done payload's `class` key (and legacy character_class)", () => {
    const s = mergeTurnCharacter(sheetFromLibrary(eda), {
      class: "Paladin",
      hp_current: 5,
      stats: { STR: 16, conditions: ["poisoned"], AC: 16 },
      inventory: { items: ["rope"] },
    });
    expect(s).toMatchObject({ className: "Paladin", hpCurrent: 5, hpMax: 12, ac: 16, conditions: ["poisoned"] });
    expect(s.inventory).toEqual([{ label: "Items", items: ["rope"] }]);
    expect(mergeTurnCharacter(null, { character_class: "Rogue" }).className).toBe("Rogue");
  });

  it("skips empty inventory groups", () => {
    expect(parseInventory({ items: [], gold: 15 })).toEqual([{ label: "Gold", items: ["15"] }]);
  });
});

describe("orderCombatants", () => {
  it("orders by initiative_order, maps the internal 'player' name, appends unlisted", () => {
    const rows = orderCombatants({
      round: 1,
      ended: false,
      outcome: null,
      initiative_order: ["Goblin", "player"],
      current_turn_index: 1,
      combatants: [
        { name: "Eda", is_player: true, hp_current: 10, hp_max: 12, alive: true },
        { name: "Goblin", is_player: false, hp_current: 0, hp_max: 7, alive: false },
        { name: "Wolf", is_player: false, hp_current: 11, hp_max: 11, alive: true },
      ],
    });
    expect(rows.map((r) => r.name)).toEqual(["Goblin", "Eda", "Wolf"]);
    expect(rows[1]).toMatchObject({ isPlayer: true, isCurrent: true });
    expect(rows[0].alive).toBe(false);
  });

  it("uses the richer initiative entries when present", () => {
    const rows = orderCombatants({
      round: 2,
      ended: false,
      outcome: null,
      initiative: [
        { name: "Eda", init: 18, is_player: true, hp: 9, hp_max: 12, ac: 16 },
        { name: "Goblin", init: 12, hp: 3, hp_max: 7, ac: 13 },
        { name: "Rat", init: 4, hp: 0, hp_max: 2, ac: 10, alive: false },
      ],
      current_turn: "Goblin",
      combatants: [{ name: "Goblin", is_player: false, hp_current: 3, hp_max: 7, alive: true }],
    });
    expect(rows.map((r) => [r.name, r.init, r.ac, r.isCurrent, r.alive])).toEqual([
      ["Eda", 18, 16, false, true],
      ["Goblin", 12, 13, true, true],
      ["Rat", 4, 10, false, false],
    ]);
  });
});

describe("roll card text", () => {
  it("formats check, expression, DC, outcome and damage", () => {
    const p = rollParts({
      success: true,
      damage: 6,
      mechanical_summary: "",
      dice_result: 17,
      dc: 15,
      modifier: 3,
      check: "Athletics",
    });
    expect(p && rollSummary(p)).toBe("Athletics · d20+3 = 17 vs DC 15 → Success · 6 dmg");
  });

  it("uses roll_expression, the AC label for attacks and flags natural 20s", () => {
    const p = rollParts({
      success: true,
      damage: null,
      mechanical_summary: "",
      dice_result: 25,
      roll_expression: "1d20+5",
      dc: 13,
      against: "AC",
      natural_roll: 20,
      check: "Longsword attack",
    });
    expect(p && rollSummary(p)).toBe("Longsword attack · 1d20+5 = 25 (nat 20) vs AC 13 → Success");
  });

  it("is hidden (null) when no dice were rolled", () => {
    expect(rollParts({ success: false, damage: null, mechanical_summary: "", dice_result: null })).toBeNull();
  });
});
