// @vitest-environment jsdom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { expect, it, vi } from "vitest";
import type { CombatState } from "../../api/types";
import { CombatHud } from "./CombatHud";

const combat = {
  round: 1,
  ended: false,
  combatants: [],
  initiative: [],
  current_turn_id: null,
  current_turn: null,
  current_turn_index: null,
  player_ac: null,
  second_wind_used: false,
  economy: null,
  raging: false,
  concentration: null,
  pending_declaration: { summary: "Confirm before any dice: Eda will close in, then attack Goblin." },
} as unknown as CombatState;

function render(node: React.ReactElement) {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  act(() => root.render(node));
  return { host, unmount: () => act(() => root.unmount()) };
}

it("shows a waiting declaration and sends yes or no as the reply", async () => {
  const onReply = vi.fn();
  const { host, unmount } = render(<CombatHud combat={combat} onReply={onReply} />);
  const group = host.querySelector('[aria-label="Confirm declaration"]');
  expect(group?.textContent).toContain("Confirm before any dice");
  const [confirm, cancel] = Array.from(group?.querySelectorAll("button") ?? []);
  await act(async () => confirm.click());
  await act(async () => cancel.click());
  expect(onReply.mock.calls).toEqual([["yes"], ["no"]]);
  unmount();
});

it("lets keyboard users read a long declaration without placing its actions inside the scrollable summary", async () => {
  const summary = "This exact encounter is above deadly. ".repeat(40);
  const onReply = vi.fn();
  const { host, unmount } = render(<CombatHud combat={{ ...combat, pending_declaration: { summary } }} onReply={onReply} />);
  const details = host.querySelector<HTMLElement>('[aria-label="Declaration summary"]')!;
  expect(details.textContent).toBe(summary);
  expect(details.tabIndex).toBe(0);
  details.focus();
  expect(document.activeElement).toBe(details);
  const actions = [...host.querySelectorAll<HTMLButtonElement>('[aria-label="Confirm declaration"] button')];
  expect(actions.map((button) => button.textContent)).toEqual(["Confirm", "Cancel and change"]);
  expect(actions.every((button) => !details.contains(button))).toBe(true);
  await act(() => actions[1].click());
  expect(onReply).toHaveBeenCalledExactlyOnceWith("no");
  unmount();
});

it("lists standing orders and cancels one by naming its foe", async () => {
  const onReply = vi.fn();
  const withOrder = { ...combat, pending_declaration: null, standing_orders: [{ foe: "Goblin", summary: "Attack Goblin each time it closes in" }] } as unknown as CombatState;
  const { host, unmount } = render(<CombatHud combat={withOrder} onReply={onReply} />);
  const list = host.querySelector('[aria-label="Standing orders"]');
  expect(list?.textContent).toContain("Attack Goblin each time it closes in");
  const button = Array.from(list?.querySelectorAll("button") ?? [])[0];
  await act(async () => button.click());
  expect(onReply.mock.calls).toEqual([["cancel the standing order against Goblin"]]);
  unmount();
});

it("shows no confirmation when none is waiting or the encounter has ended", () => {
  const { host, unmount } = render(<CombatHud combat={{ ...combat, pending_declaration: null }} />);
  expect(host.querySelector('[aria-label="Confirm declaration"]')).toBeNull();
  unmount();
  const ended = render(<CombatHud combat={{ ...combat, ended: true }} />);
  expect(ended.host.querySelector('[aria-label="Confirm declaration"]')).toBeNull();
  ended.unmount();
});

it("keeps the engine's exact-creature danger visible in compact combat and supports older saves", () => {
  const text = "This exact foe is far above deadly for a level-1 solo hero. Its statistics are unchanged.";
  const { host, unmount } = render(<CombatHud compact showDeclaration={false} combat={{ ...combat, solo_budget: { warning: { tier: "far_above_deadly", text } } }} />);
  expect(host.textContent).toContain(text);
  expect(host.textContent).toContain("Danger: far above deadly");
  expect(host.querySelector('[aria-label="Confirm declaration"]')).toBeNull();
  unmount();
  const legacy = render(<CombatHud compact combat={{ ...combat, solo_budget: null }} />);
  expect(legacy.host.textContent).not.toContain("Danger:");
  legacy.unmount();
});
