/** EDG-034: resource counters, supported effects and healing rolls, from engine payloads only. */
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import type { AdjudicationResult, CharacterAssignmentHistoryRead, CombatState, TurnResult } from "../../api/types";
import { CombatHud } from "./CombatHud";
import {
  mechanicalEffects,
  mergeTurnCharacter,
  orderCombatants,
  parseSecondWind,
  parseSpellSlots,
  sheetForCampaign,
  sheetFromLibrary,
} from "./gameState";
import { ResourceCounters } from "./ResourceCounters";
import { RollCard } from "./RollCard";
import { healingRolls } from "./rollText";

const fighterAssignment: CharacterAssignmentHistoryRead = {
  id: 1,
  campaign_id: 3,
  campaign_title: "Mines",
  hp_current: 9,
  hp_max: 12,
  stats: { STR: 16 },
  inventory: { items: ["rope"] },
  assigned_at: "2026-10-01",
  ended_at: null,
  status: "active",
  second_wind: { uses_remaining: 1, uses_max: 1 },
  spell_slots: null,
};

const eda = {
  id: 1,
  owner_user_id: 1,
  name: "Eda",
  character_class: "Fighter",
  level: 3,
  hp_max: 12,
  base_stats: {},
  base_inventory: {},
  created_at: "",
};

describe("resource derivations", () => {
  it("reads Second Wind and spell slots from the assignment and clamps out-of-range counts", () => {
    expect(parseSecondWind({ uses_remaining: 5, uses_max: 1 })).toEqual({ uses_remaining: 1, uses_max: 1 });
    expect(parseSecondWind({ uses_remaining: -2, uses_max: 1 })).toEqual({ uses_remaining: 0, uses_max: 1 });
    expect(parseSecondWind({ uses_remaining: "lots", uses_max: 1 })).toBeNull();
    expect(parseSecondWind({ uses_remaining: 1 })).toBeNull();
    expect(parseSpellSlots({ "2": { total: 2, remaining: 1 }, "1": { total: 4, remaining: 9 } })).toEqual([
      { level: 1, total: 4, remaining: 4 },
      { level: 2, total: 2, remaining: 1 },
    ]);
    expect(parseSpellSlots({ "0": { total: 1, remaining: 1 }, nope: { total: 1, remaining: 1 } })).toBeNull();

    const sheet = sheetForCampaign(eda, [fighterAssignment], 3);
    expect(sheet.secondWind).toEqual({ uses_remaining: 1, uses_max: 1 });
    expect(sheet.spellSlots).toBeNull();
  });

  it("shows nothing for legacy assignments that carry no resource fields", () => {
    const legacy = { ...fighterAssignment, second_wind: undefined, spell_slots: undefined };
    const sheet = sheetForCampaign(eda, [legacy], 3);
    expect(sheet.secondWind).toBeNull();
    expect(sheet.spellSlots).toBeNull();
    expect(renderToStaticMarkup(<ResourceCounters secondWind={sheet.secondWind} spellSlots={sheet.spellSlots} />)).toBe("");
  });

  it("replaces counters from each done payload, so a spend shows at once and a legacy payload keeps the last value", () => {
    const base = sheetForCampaign(eda, [fighterAssignment], 3);
    const spent = mergeTurnCharacter(base, {
      second_wind: { uses_remaining: 0, uses_max: 1 },
      spell_slots: { "1": { total: 4, remaining: 3 } },
    });
    expect(spent.secondWind).toEqual({ uses_remaining: 0, uses_max: 1 });
    expect(spent.spellSlots).toEqual([{ level: 1, total: 4, remaining: 3 }]);

    // A legacy payload without the keys does not erase what the sheet already knows.
    expect(mergeTurnCharacter(spent, { hp_current: 4 }).secondWind).toEqual({ uses_remaining: 0, uses_max: 1 });

    // A later payload that recharges the resource replaces the stale spent value.
    expect(mergeTurnCharacter(spent, { second_wind: { uses_remaining: 1, uses_max: 1 } }).secondWind).toEqual({
      uses_remaining: 1,
      uses_max: 1,
    });
  });

  it("renders counts as text, with no Hit Dice counter", () => {
    const html = renderToStaticMarkup(
      <ResourceCounters
        secondWind={{ uses_remaining: 0, uses_max: 1 }}
        spellSlots={[{ level: 1, total: 4, remaining: 3 }]}
      />,
    );
    expect(html).toContain("Second Wind");
    expect(html).toContain("0 of 1 use left");
    expect(html).toContain('aria-label="Spell slots"');
    expect(html).toContain("Level 1 slots");
    expect(html).toContain("3 of 4 left");
    expect(html.toLowerCase()).not.toContain("hit di");
  });
});

describe("supported effects and disposition", () => {
  it("keeps only engine-supported effect kinds and never a narrative tag", () => {
    expect(
      mechanicalEffects([
        { kind: "restrained", source: "a net", applied_round: 1 },
        { kind: "restrained", source: "a net", applied_round: 2 },
        { kind: "prone", source: "a shove", applied_round: 1 },
        { kind: "frightened", source: "narration", applied_round: 1 },
        { kind: "constructor", source: "x", applied_round: 1 },
      ]),
    ).toEqual([
      { kind: "restrained", label: "Restrained" },
      { kind: "prone", label: "Prone" },
    ]);
    expect(mechanicalEffects(undefined)).toEqual([]);
  });

  it("maps roster and initiative effects and ignores legacy entries without them", () => {
    const combat: CombatState = {
      round: 2,
      ended: false,
      outcome: null,
      current_turn: "Goblin",
      initiative: [
        { name: "Goblin", init: 14, is_player: false, hp: 3, hp_max: 7, disposition: "active", effects: [{ kind: "restrained", source: "a net", applied_round: 1 }] },
        { name: "Eda", init: 12, is_player: true, hp: 9, hp_max: 12, disposition: "active" },
      ],
      combatants: [
        { name: "Goblin", is_player: false, hp_current: 3, hp_max: 7, alive: true, disposition: "active", effects: [{ kind: "restrained", source: "a net", applied_round: 1 }] },
        { name: "Eda", is_player: true, hp_current: 9, hp_max: 12, alive: true, disposition: "active" },
      ],
    };
    const rows = orderCombatants(combat);
    expect(rows[0].effects).toEqual([{ kind: "restrained", label: "Restrained" }]);
    expect(rows[1].effects).toEqual([]);
  });

  it("renders disposition and effects as text in the HUD", () => {
    const combat: CombatState = {
      round: 1,
      ended: false,
      outcome: null,
      combatants: [
        { name: "Goblin", is_player: false, hp_current: 0, hp_max: 7, alive: false, disposition: "captured", effects: [{ kind: "prone", source: "a shove", applied_round: 1 }] },
        { name: "Ogre", is_player: false, hp_current: 0, hp_max: 20, alive: false, disposition: "dead", effects: [] },
      ],
    };
    const html = renderToStaticMarkup(<CombatHud combat={combat} />);
    expect(html).toContain("captive");
    expect(html).toContain("dead");
    expect(html).toContain('aria-label="Goblin active effects"');
    expect(html).toContain("Prone");
    expect(html).not.toContain("Ogre active effects");
  });
});

describe("healing rolls", () => {
  const second: AdjudicationResult = {
    success: true,
    damage: null,
    mechanical_summary: "",
    dice_result: null,
    dice_log: [
      { expression: "1d10+1", rolls: [6], modifier: 1, total: 7, purpose: "second wind" },
      { expression: "1d8", rolls: [3], modifier: 0, total: 3, purpose: "Cure Wounds healing" },
      { expression: "1d6", rolls: [4], modifier: 0, total: 4, purpose: "athletics check" },
    ],
  };

  it("lists only healing dice, with the rolled faces and total", () => {
    expect(healingRolls(second)).toEqual([
      { label: "Second Wind", expression: "1d10+1", rolls: [6], total: 7 },
      { label: "Cure Wounds healing", expression: "1d8", rolls: [3], total: 3 },
    ]);
    expect(healingRolls(null)).toEqual([]);
  });

  it("renders healing dice even without a check result, and says it is a roll", () => {
    const html = renderToStaticMarkup(<RollCard adj={second} />);
    expect(html).toContain("Second Wind · 1d10+1 [6] = 7 healing");
    expect(html).toContain("Cure Wounds healing · 1d8 [3] = 3 healing");
    expect(html).not.toContain("Success");
  });

  it("keeps a negative healing roll's total but says it heals nothing", () => {
    const weak: AdjudicationResult = {
      ...second,
      dice_log: [{ expression: "1d8-2", rolls: [1], modifier: -2, total: -1, purpose: "Cure Wounds healing" }],
    };
    expect(healingRolls(weak)).toEqual([
      { label: "Cure Wounds healing", expression: "1d8-2", rolls: [1], total: -1 },
    ]);
    expect(renderToStaticMarkup(<RollCard adj={weak} />)).toContain(
      "Cure Wounds healing · 1d8-2 [1] = -1, heals 0",
    );
  });

  it("renders nothing for a turn with no dice", () => {
    expect(renderToStaticMarkup(<RollCard adj={null} />)).toBe("");
  });
});

// Keep the done-payload type honest: a character block may carry the resource fields.
const donePayload: TurnResult = {
  narration: "You catch your breath.",
  character: { id: 1, name: "Eda", class: "Fighter", level: 3, second_wind: { uses_remaining: 0, uses_max: 1 } },
};

describe("done payload", () => {
  it("carries resource fields that the sheet merges", () => {
    const merged = mergeTurnCharacter(sheetFromLibrary(eda), donePayload.character!);
    expect(merged.secondWind).toEqual({ uses_remaining: 0, uses_max: 1 });
  });
});
