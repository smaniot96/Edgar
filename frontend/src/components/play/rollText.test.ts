import { describe, expect, it } from "vitest";

import type { RollRecord } from "../../api/types";
import { adjudicationFromRoll, rollParts, rollSummary } from "./rollText";

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
  it("renders both disadvantage faces and the kept natural 1 exactly as stored", () => {
    const parts = rollParts(adjudicationFromRoll(disadvantage));
    expect(parts).toMatchObject({ check: "athletics", total: 6, dc: 15, natural: "nat 1", faces: [1, 12], mode: "disadvantage", success: false, damage: 3 });
    expect(parts && rollSummary(parts)).toBe("athletics · 1d20+5 [1, 12] = 6 (nat 1) vs DC 15 → Failure · 3 dmg");
  });

  it("copies only the card fields, so no private ruling data is read", () => {
    const adj = adjudicationFromRoll(disadvantage);
    expect(Object.keys(adj).sort()).toEqual(
      ["check", "conditions", "against", "damage", "dc", "dice_log", "dice_result", "mechanical_summary", "modifier", "natural_roll", "roll_expression", "success"].sort(),
    );
  });
});
