import { describe, expect, it } from "vitest";

import type { RollRecord } from "../../api/types";
import { adjudicationFromRoll, multipleAttackRolls, restSummary, rollParts, rollSummary } from "./rollText";

const disadvantage: RollRecord = {
  turn_id: "t",
  check: "athletics",
  success: false,
  dc: 15,
  against: "DC",
  modifier: 5,
  roll_expression: "1d20+5",
  natural_roll: 1,
  dice_result: 6,
  damage: 3,
  healing: null,
  conditions: ["prone"],
  mechanical_summary: "Athletics at disadvantage.",
  dice_log: [
    { expression: "1d20", rolls: [1, 12], modifier: 0, total: 1, purpose: "athletics", roll_mode: "disadvantage", kept: 1 },
  ],
};

describe("adjudicationFromRoll", () => {
  it("separates Action Surge rolls without attaching aggregate damage to the miss", () => {
    const adj = adjudicationFromRoll({ ...disadvantage, check: "attack", dc: 14, against: "AC", damage: 7,
      dice_log: [
        { expression: "1d20+5", rolls: [18], modifier: 5, total: 23, purpose: "half-orc attack" },
        { expression: "1d20+6", rolls: [3], modifier: 6, total: 9, purpose: "attack check" },
        { expression: "1d20+6", rolls: [8], modifier: 6, total: 14, purpose: "attack check" },
        { expression: "1d8+4", rolls: [3], modifier: 4, total: 7, purpose: "player damage" },
      ],
    });
    const attacks = multipleAttackRolls(adj);
    expect(attacks.map(({ adjudication }) => rollParts(adjudication))).toMatchObject([
      { check: "Attack 1", total: 9, success: false, damage: null },
      { check: "Attack 2", total: 14, success: true, damage: null },
    ]);
    expect(attacks.map(({ index }) => index)).toEqual([1, 2]);
    expect(multipleAttackRolls(adjudicationFromRoll(disadvantage))).toEqual([]);
  });
  it("renders both disadvantage faces and the kept natural 1 exactly as stored", () => {
    const parts = rollParts(adjudicationFromRoll(disadvantage));
    expect(parts).toMatchObject({ check: "athletics", total: 6, dc: 15, natural: "nat 1", faces: [1, 12], mode: "disadvantage", success: false, damage: 3 });
    expect(parts && rollSummary(parts)).toBe("athletics · 1d20+5 [1, 12] = 6 (nat 1) vs DC 15 → Failure · 3 dmg");
  });

  it("copies only the card fields, so no private ruling data is read", () => {
    const adj = adjudicationFromRoll(disadvantage);
    expect(Object.keys(adj).sort()).toEqual(
      ["check", "conditions", "against", "damage", "dc", "dice_log", "dice_result", "mechanical_summary", "modifier", "natural_roll", "rest_record", "roll_expression", "success"].sort(),
    );
  });
});

const shortRest: RollRecord = {
  turn_id: "rest-1",
  check: null,
  success: true,
  dc: null,
  against: null,
  modifier: null,
  roll_expression: null,
  natural_roll: null,
  dice_result: null,
  damage: null,
  healing: 7,
  conditions: [],
  mechanical_summary: "",
  dice_log: [{ expression: "1d10+2", rolls: [5], modifier: 2, total: 7, purpose: "short rest hit die" }],
  rest: {
    kind: "short",
    refused: false,
    expression: "1d10+2",
    rolled: 7,
    healed: 7,
    die: "d10",
    hit_dice_remaining: 1,
    hit_dice_total: 2,
    hit_dice_recovered: null,
  },
};

describe("rest evidence (EDG-026)", () => {
  it("shows the rolled die, the healing applied and the Hit Dice left", () => {
    expect(restSummary(shortRest.rest!)).toBe("Short rest · 1d10+2 = 7 · healed 7 HP · Hit Dice 1/2 d10 left");
  });

  it("says a refused rest rolled nothing and healed nothing", () => {
    const refused = { ...shortRest.rest!, refused: true, expression: null, rolled: null, healed: 0, hit_dice_remaining: 0, hit_dice_total: 1 };
    expect(restSummary(refused)).toBe("Short rest · no Hit Dice left · no healing · Hit Dice 0/1 d10 left");
  });

  it("names the Hit Dice a long rest recovers, and omits the count when none came back", () => {
    const long = { ...shortRest.rest!, kind: "long" as const, expression: null, rolled: null, healed: 5, hit_dice_recovered: 1, hit_dice_remaining: 2, hit_dice_total: 2 };
    expect(restSummary(long)).toBe("Long rest · HP restored · recovered 1 Hit Die · Hit Dice 2/2 d10 left");
    expect(restSummary({ ...long, hit_dice_recovered: 0 })).toBe("Long rest · HP restored · Hit Dice 2/2 d10 left");
  });

  it("copies the stored rest into the card input, with no check to render", () => {
    const adj = adjudicationFromRoll(shortRest);
    expect(adj.rest_record).toEqual(shortRest.rest);
    expect(rollParts(adj)).toBeNull();
  });
});

