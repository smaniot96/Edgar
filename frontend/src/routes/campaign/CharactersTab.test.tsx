// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  listCampaignCharacters: vi.fn(),
  levelUpCampaignCharacter: vi.fn(),
}));
vi.mock("../../api/client", () => ({
  listCharacters: vi.fn().mockResolvedValue([]),
  assignCharacterToCampaign: vi.fn(),
  listCampaignCharacters: mocks.listCampaignCharacters,
  levelUpCampaignCharacter: mocks.levelUpCampaignCharacter,
}));

import { ModeProvider } from "../../dev/ModeProvider";
import CharactersTab from "./CharactersTab";

type Row = {
  assignment_id: number;
  character_id: number;
  name: string;
  character_class: string;
  level: number;
  hp_current: number;
  hp_max: number;
  milestones_pending?: number;
  life_state?: "alive" | "dying" | "stable" | "dead";
};

const hero = (overrides: Partial<Row> = {}): Row => ({
  assignment_id: 7,
  character_id: 3,
  name: "Eda",
  character_class: "Fighter",
  level: 1,
  hp_current: 12,
  hp_max: 12,
  milestones_pending: 1,
  life_state: "alive",
  ...overrides,
});

let host: HTMLDivElement;
let root: Root;

beforeEach(() => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  mocks.listCampaignCharacters.mockReset();
  mocks.levelUpCampaignCharacter.mockReset();
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
});

afterEach(() => {
  act(() => root.unmount());
  host.remove();
});

async function render(props: { progressionMode?: "fixed" | "milestone"; rows: Row[] }) {
  mocks.listCampaignCharacters.mockResolvedValue(props.rows);
  await act(async () => {
    root.render(
      <ModeProvider>
        <MemoryRouter>
          <CharactersTab campaignId={9} progressionMode={props.progressionMode} />
        </MemoryRouter>
      </ModeProvider>,
    );
  });
  // Let the query resolve and React commit it.
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

const text = () => host.textContent ?? "";
const button = (label: string) =>
  Array.from(host.querySelectorAll("button")).find((b) => b.textContent?.includes(label)) ?? null;

it("offers a level-up only in a milestone campaign with a milestone to spend", async () => {
  await render({ progressionMode: "fixed", rows: [hero()] });
  expect(text()).toContain("Eda");
  expect(button("Level up")).toBeNull();
});

it("spends a milestone with the level the sheet shows and reports what was gained", async () => {
  mocks.levelUpCampaignCharacter.mockResolvedValue({
    applied: true,
    from_level: 1,
    to_level: 2,
    hit_points: { amount: 8, method: "average", dice: null, hp_max_before: 12, hp_max_after: 20 },
    ability_increases: {},
    features: [{ name: "Action Surge", executed: true }],
    spell_slots: {},
    milestones_pending: 0,
    character: hero({ level: 2, hp_max: 20, hp_current: 20, milestones_pending: 0 }),
  });
  mocks.listCampaignCharacters.mockResolvedValueOnce([hero()]).mockResolvedValue([hero({ level: 2, milestones_pending: 0 })]);
  await act(async () => {
    root.render(
      <ModeProvider>
        <MemoryRouter>
          <CharactersTab campaignId={9} progressionMode="milestone" />
        </MemoryRouter>
      </ModeProvider>,
    );
  });
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });

  const levelUp = button("Level up to 2");
  expect(levelUp).not.toBeNull();
  await act(async () => {
    levelUp!.click();
  });
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });

  const [campaignId, characterId, body] = mocks.levelUpCampaignCharacter.mock.calls[0];
  expect([campaignId, characterId]).toEqual([9, 3]);
  // Level 1 to 2 is not the ability step: no score choice is sent, and the id is unique per click.
  expect(body).toMatchObject({ from_level: 1, hp_method: "average" });
  expect(body.ability_increases).toBeUndefined();
  expect(typeof body.request_id).toBe("string");
  expect(text()).toContain("maximum HP 12 to 20");
  expect(text()).toContain("Running now: Action Surge");
});

it("asks for the ability score improvement at level 4 and sends it as two points", async () => {
  mocks.levelUpCampaignCharacter.mockResolvedValue({
    applied: true,
    from_level: 3,
    to_level: 4,
    hit_points: { amount: 8, method: "average", dice: null, hp_max_before: 20, hp_max_after: 28 },
    ability_increases: { DEX: 2 },
    features: [{ name: "Ability Score Improvement", executed: true }],
    spell_slots: {},
    milestones_pending: 0,
    character: hero({ level: 4 }),
  });
  mocks.listCampaignCharacters.mockResolvedValue([hero({ level: 3 })]);
  await render({ progressionMode: "milestone", rows: [hero({ level: 3 })] });

  const select = host.querySelector("li select") as HTMLSelectElement | null;
  expect(select).not.toBeNull();
  await act(async () => {
    select!.value = "DEX";
    select!.dispatchEvent(new Event("change", { bubbles: true }));
  });
  await act(async () => {
    button("Level up to 4")!.click();
  });
  await act(async () => {
    await Promise.resolve();
  });
  const [, , body] = mocks.levelUpCampaignCharacter.mock.calls[0];
  expect(body).toMatchObject({ from_level: 3, ability_increases: { DEX: 2 } });
});

it("shows a dead hero's state and offers no level-up", async () => {
  await render({ progressionMode: "milestone", rows: [hero({ life_state: "dead" })] });
  expect(text()).toContain("Dead: a permanent loss");
  expect(button("Level up")).toBeNull();
});

it("shows a dying hero's state as a death-save state", async () => {
  await render({ progressionMode: "milestone", rows: [hero({ life_state: "dying", milestones_pending: 0 })] });
  expect(text()).toContain("Dying: making death saving throws");
});
