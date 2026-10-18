/** EDG-173: the sheet shows the engine's AC, spells and proficiencies in plain words, never raw flags. */
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import type { CharacterAssignmentHistoryRead } from "../../api/types";
import { CharacterSheet } from "./CharacterSheet";
import { sheetFromLibrary, sheetForCampaign } from "./gameState";

const cleric: CharacterAssignmentHistoryRead = {
  id: 1,
  campaign_id: 3,
  campaign_title: "Mines",
  hp_current: 8,
  hp_max: 8,
  stats: { STR: 13, DEX: 10, CON: 14, INT: 10, WIS: 16, CHA: 12 },
  inventory: { weapons: ["mace"], armor: "scale mail", holy_symbol: true },
  assigned_at: "2026-10-01",
  ended_at: null,
  status: "active",
  armor_class: 14,
  spells: [
    { name: "Sacred Flame", level: 0, action: "action" },
    { name: "Healing Word", level: 1, action: "bonus_action" },
  ],
  proficiencies: {
    saving_throws: [
      { ability: "WIS", modifier: 5 },
      { ability: "CHA", modifier: 3 },
    ],
    skills: [{ skill: "sleight_of_hand", modifier: 2 }],
  },
};

const eda = {
  id: 2,
  owner_user_id: 1,
  name: "Brin",
  character_class: "Cleric",
  level: 1,
  hp_max: 8,
  base_stats: {},
  base_inventory: {},
  created_at: "",
};

function render(assignment: CharacterAssignmentHistoryRead) {
  const character = sheetForCampaign(eda, [assignment], 3);
  return renderToStaticMarkup(
    <CharacterSheet character={character} open onClose={() => {}} />,
  );
}

describe("character sheet EDG-173 view", () => {
  it("shows the engine's armour class", () => {
    const html = render(cleric);
    expect(html).toContain('title="Armor class"');
    expect(html).toContain(">14<");
  });

  it("lists spells with slot costs and never a raw boolean", () => {
    const html = render(cleric);
    expect(html).toContain("Sacred Flame");
    expect(html).toContain("Cantrip");
    expect(html).toContain("Healing Word");
    expect(html).toContain("1st-level slot");
    expect(html).toContain("bonus action");
    expect(html).not.toMatch(/>true</);
  });

  it("shows proficiencies with their modifiers in plain words", () => {
    const html = render(cleric);
    expect(html).toContain("Saving throws");
    expect(html).toContain("WIS");
    expect(html).toContain("+5");
    expect(html).toContain("Sleight of hand");
    expect(html).toContain("+2");
  });

  it("renders the holy symbol flag as an item", () => {
    const html = render(cleric);
    expect(html).toContain("Holy symbol");
    expect(html).not.toMatch(/>true</);
  });

  it("omits the spell list for a fighter with no castable spells", () => {
    const fighter: CharacterAssignmentHistoryRead = {
      ...cleric,
      stats: {},
      inventory: { weapons: ["longsword"], armor: "chain mail" },
      armor_class: 18,
      spells: null,
      proficiencies: { saving_throws: [], skills: [] },
    };
    const html = render(fighter);
    expect(html).not.toContain("Spells");
    expect(html).toContain(">18<");
  });

  it("builds the sheet fields from the assignment", () => {
    const sheet = sheetFromLibrary(eda, cleric);
    expect(sheet.ac).toBe(14);
    expect(sheet.spells?.map((s) => s.name)).toEqual(["Sacred Flame", "Healing Word"]);
    expect(sheet.proficiencies?.skills).toEqual([{ skill: "sleight_of_hand", modifier: 2 }]);
  });
});
