import { describe, expect, it } from "vitest";

import type { CharacterAssignmentHistoryRead, CharacterRead } from "../../api/types";
import {
  economyLine,
  hitDiceFrom,
  mechanicalEffects,
  mergeTurnCharacter,
  orderCombatants,
  parseInventory,
  sheetForCampaign,
  sheetFromLibrary,
} from "./gameState";
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

  it("uses an ended campaign's saved HP and equipment after the hero moves to another campaign", () => {
    const past: CharacterAssignmentHistoryRead = {
      id: 1, campaign_id: 3, campaign_title: "Past", hp_current: 4, hp_max: 12,
      stats: { STR: 16, conditions: ["poisoned"] }, inventory: { items: ["silver key"] },
      assigned_at: "2026-10-01", ended_at: "2026-10-02", status: "ended",
    };
    const current = { ...past, id: 2, campaign_id: 4, status: "active", hp_current: 12,
      assigned_at: "2026-10-03", ended_at: null, inventory: { items: ["rope"] } };
    const sheet = sheetForCampaign(eda, [current, past], 3);
    expect(sheet.hpCurrent).toBe(4);
    expect(sheet.conditions).toEqual(["poisoned"]);
    expect(sheet.inventory).toEqual([{ label: "Items", items: ["silver key"] }]);
    expect(sheetForCampaign(eda, [current], 3)).toEqual(sheetFromLibrary(eda));
  });

  it("prefers the active assignment when a reopened campaign has older assignment records", () => {
    const assignment: CharacterAssignmentHistoryRead = {
      id: 1, campaign_id: 3, campaign_title: "Reopened", hp_current: 8, hp_max: 12,
      stats: {}, inventory: {}, assigned_at: "2026-10-01", ended_at: null, status: "active",
    };
    const ended = { ...assignment, id: 2, hp_current: 2, status: "ended", assigned_at: "2026-10-02" };
    expect(sheetForCampaign(eda, [ended, assignment], 3).hpCurrent).toBe(8);
    expect(sheetForCampaign(eda, [ended, { ...ended, id: 3, assigned_at: "2026-10-03", hp_current: 6 }], 3).hpCurrent).toBe(6);
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

describe("combat disposition in the HUD", () => {
  it("keeps a living captive distinct from a standing foe and a dead one", () => {
    const rows = orderCombatants({
      round: 3,
      ended: false,
      outcome: null,
      combatants: [],
      initiative: [
        { name: "Bandit", init: 14, hp: 0, hp_max: 7, ac: 12, alive: true, disposition: "unconscious", can_act: false },
        { name: "Wolf", init: 9, hp: 11, hp_max: 11, ac: 13, alive: true, disposition: "active", can_act: true },
        { name: "Rat", init: 4, hp: 0, hp_max: 2, ac: 10, alive: false, disposition: "dead", can_act: false },
      ],
    });
    expect(rows.map((r) => [r.name, r.alive, r.disposition, r.canAct])).toEqual([
      ["Bandit", true, "unconscious", false],
      ["Wolf", true, "active", true],
      ["Rat", false, "dead", false],
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

  it("shows both d20 faces when advantage applied and the kept face", () => {
    const p = rollParts({
      success: true,
      damage: null,
      mechanical_summary: "",
      dice_result: 22,
      roll_expression: "2d20kh1+5",
      dc: 15,
      natural_roll: 17,
      check: "Athletics",
      dice_log: [
        { expression: "2d20+5", rolls: [4, 17], modifier: 5, total: 22, roll_mode: "advantage", kept: 17 },
      ],
    });
    expect(p && rollSummary(p)).toBe("Athletics · 2d20kh1+5 [4, 17] = 22 vs DC 15 → Success");
  });

  it("does not show faces for a plain roll or an unrelated log entry", () => {
    const p = rollParts({
      success: false,
      damage: null,
      mechanical_summary: "",
      dice_result: 9,
      roll_expression: "1d20+5",
      dc: 15,
      check: "Athletics",
      dice_log: [{ expression: "1d8", rolls: [4, 5], modifier: 0, total: 9, purpose: "damage" }],
    });
    expect(p && rollSummary(p)).toBe("Athletics · 1d20+5 = 9 vs DC 15 → Failure");
  });

  it("is hidden (null) when no dice were rolled", () => {
    expect(rollParts({ success: false, damage: null, mechanical_summary: "", dice_result: null })).toBeNull();
  });
});

describe("combat economy (EDG-044)", () => {
  it("reads the hero's last turn and stays silent for legacy payloads", () => {
    expect(economyLine({ round: 2, action: "attack", bonus_action: "Second Wind", dodging: false })).toBe(
      "Last turn: action attack · bonus action Second Wind",
    );
    expect(economyLine({ round: 1, action: "Dodge", bonus_action: null, dodging: true })).toBe(
      "Last turn: action Dodge · bonus action unused",
    );
    expect(economyLine(null)).toBeNull();
    expect(economyLine(undefined)).toBeNull();
  });

  it("labels Dodging as a mechanical effect and drops unknown kinds", () => {
    expect(mechanicalEffects([{ kind: "dodging" }, { kind: "stunned" }])).toEqual([
      { kind: "dodging", label: "Dodging" },
    ]);
  });
});
describe("Hit Dice on the sheet (EDG-026)", () => {
  const assignment: CharacterAssignmentHistoryRead = {
    id: 9, campaign_id: 3, campaign_title: "Quay", hp_current: 5, hp_max: 12,
    stats: { hit_dice: { spent: 1 } }, inventory: {}, hit_dice: { remaining: 0, total: 1, die: "d10" },
    assigned_at: "2026-10-01", ended_at: null, status: "active",
  };

  it("reads the server's view from the assignment and the turn payload", () => {
    const sheet = sheetFromLibrary(eda, assignment);
    expect(sheet.hitDice).toEqual({ remaining: 0, total: 1, die: "d10" });
    expect(sheetFromLibrary(eda).hitDice).toBeNull();
    expect(mergeTurnCharacter(sheet, { hit_dice: { remaining: 1, total: 1, die: "d10" } }).hitDice).toEqual({
      remaining: 1, total: 1, die: "d10",
    });
    // A turn payload without the field keeps the last known dice.
    expect(mergeTurnCharacter(sheet, { hp_current: 3 }).hitDice).toEqual({ remaining: 0, total: 1, die: "d10" });
  });

  it("ignores a malformed Hit Dice view rather than showing it", () => {
    expect(hitDiceFrom({ remaining: "two", total: 2, die: "d8" })).toBeNull();
    expect(hitDiceFrom({ remaining: 1, total: 2 })).toBeNull();
    expect(hitDiceFrom(null)).toBeNull();
  });
});

